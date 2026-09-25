# ==============================================================================
# Paperclip — AI Company Orchestration Platform
# ==============================================================================
# Builds directly from paperclipai/paperclip source at the pinned release tag.
# Strategy: clone the tag, then use the upstream Dockerfile from that tag.
# This ensures the COPY structure exactly matches the source tree.
#
# Build arg PAPERCLIP_TAG controls which release to build.
# Default: v2026.626.0 (bumped from v2026.609.0 — 2026.07, #36 / PR #84).
# ==============================================================================

ARG PAPERCLIP_TAG=v2026.707.0

# ── Stage 1: Clone source at pinned tag ──────────────────────────────────────
# rc6.7 #77: was `alpine/git:2.47` — that bare-major tag was retired
# upstream, manifest pull fails. Pin to v2.47.2 (matching cognee/frontend's
# Dockerfile) so the build is reproducible.
FROM alpine/git:v2.47.2 AS source
ARG PAPERCLIP_TAG
WORKDIR /src
RUN git clone --depth 1 --branch ${PAPERCLIP_TAG} \
    https://github.com/paperclipai/paperclip.git .

# ── Stage 2: base — system dependencies ──────────────────────────────────────
FROM node:lts-trixie-slim AS base
ARG USER_UID=1000
ARG USER_GID=1000
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
       ca-certificates gosu curl git wget ripgrep python3 procps \
  && mkdir -p -m 755 /etc/apt/keyrings \
  && wget -nv -O/etc/apt/keyrings/githubcli-archive-keyring.gpg \
       https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && mkdir -p -m 755 /etc/apt/sources.list.d \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends gh \
  && rm -rf /var/lib/apt/lists/* \
  && corepack enable

RUN usermod -u $USER_UID --non-unique node \
  && groupmod -g $USER_GID --non-unique node \
  && usermod -g $USER_GID -d /paperclip node

# ── Stage 3: Install deps — copy full source so pnpm install discovers every workspace package ─────────
FROM base AS deps
WORKDIR /app
# The previous "copy each package.json by name" approach was an optimization
# for cache hits when only source files (not package.jsons) changed.
# But it requires the COPY list to stay in lockstep with upstream paperclip's
# workspace structure. Twice in v2026.05-ga.4 hotfix work the upstream added
# new adapter packages (`adapter-utils`, `acpx-local`, `cursor-cloud`)
# without us updating the list, causing pnpm install to skip those packages'
# node_modules → `pnpm -r build` failed with TS2307 / TS2688 errors. Copying
# the full src once is one extra MB of layer per build but removes the
# maintenance burden entirely; pnpm install --frozen-lockfile reads
# `pnpm-workspace.yaml` and resolves whatever's on disk.
COPY --from=source /src ./

RUN pnpm install --frozen-lockfile

# ── Stage 4: Build ───────────────────────────────────────────────────────────
FROM base AS build
WORKDIR /app
COPY --from=deps /app /app
COPY --from=source /src .
# Use `pnpm -r build` so all workspace packages build in dependency order,
# not just ui/plugin-sdk/server. Without this, paperclip v2026.513.0 fails
# at `tsc -b && vite build` in @paperclipai/ui with TS2307 "Cannot find
# module '@paperclipai/adapter-utils'" — the package.json is symlinked
# correctly by `pnpm install` (deps stage), but adapter-utils' compiled
# dist/types aren't produced because no build runs for it under the
# `--filter @paperclipai/ui` scope. Recursive build respects the workspace
# dependency graph (adapter-utils builds before ui automatically) and is
# resilient to upstream paperclip adding more workspace packages.
RUN pnpm -r build
RUN test -f server/dist/index.js || (echo "ERROR: server build output missing" && exit 1)

# ── Stage 5: Production image ─────────────────────────────────────────────────
FROM base AS production
ARG USER_UID=1000
ARG USER_GID=1000
WORKDIR /app
COPY --chown=node:node --from=build /app /app
RUN npm install --global --omit=dev \
      @anthropic-ai/claude-code@latest \
      @openai/codex@latest \
      @google/gemini-cli@latest \
      opencode-ai \
      gsd-pi \
  && mkdir -p /paperclip \
  && chown node:node /paperclip

COPY --from=source /src/scripts/docker-entrypoint.sh /usr/local/bin/
COPY configure-opencode.sh /usr/local/bin/configure-opencode.sh
COPY entrypoint-wrapper.sh /usr/local/bin/entrypoint-wrapper.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
              /usr/local/bin/configure-opencode.sh \
              /usr/local/bin/entrypoint-wrapper.sh

ENV NODE_ENV=production \
    HOME=/paperclip \
    HOST=0.0.0.0

VOLUME ["/paperclip"]
EXPOSE 3100

ENTRYPOINT ["entrypoint-wrapper.sh"]
CMD ["node", "--import", "./server/node_modules/tsx/dist/loader.mjs", "server/dist/index.js"]
