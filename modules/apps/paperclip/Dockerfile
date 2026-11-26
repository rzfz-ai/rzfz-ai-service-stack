# ==============================================================================
# Paperclip — AI Company Orchestration Platform
# ==============================================================================
# Builds directly from paperclipai/paperclip source at the pinned release tag.
# Strategy: clone the tag, then use the upstream Dockerfile from that tag.
# This ensures the COPY structure exactly matches the source tree.
#
# Build arg PAPERCLIP_TAG controls which release to build.
# Default: v2026.707.0. #2336: do NOT bump past this yet. v2026.1012.0 cannot
# build here (its upstream pnpm lockfile is stale, so `pnpm install
# --frozen-lockfile` fails) and newer tags need upstream's Rust runner stage;
# that rework is 2027.01 work. The 4 Critical @paperclipai/server findings on
# this tag were false positives from the 0.3.1 package.json, fixed by the version
# stamp below (the fixed server is >= 2026.416.0).
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
# #2336: the source tree keeps the workspace packages at 0.3.1 (server and the
# paperclipai CLI alike), while the published @paperclipai/server carries the release's calendar version (the
# advisories are ranged on that, fixed >= 2026.416.0). Stamp the tag's version so
# the built image states what it is; a scanner matching "0.3.1" against the
# advisory ranges would otherwise flag a build that already has the fix.
RUN v="${PAPERCLIP_TAG#v}" \
  && for f in server/package.json cli/package.json; do \
       sed -i -E "s/^  \"version\": *\"[^\"]+\"/  \"version\": \"${v}\"/" "$f" \
       && grep -q "\"version\": \"${v}\"" "$f" || exit 1; \
     done

# ── Stage 2: base — system dependencies ──────────────────────────────────────
# #2946: was the floating `node:lts-trixie-slim`. `lts` moved to Node 26, which no
# longer bundles corepack, so `corepack enable` below failed (corepack: not found)
# and no image or offline package could be built. Node 24 still ships corepack;
# upstream's engines is only node >= 20 and packageManager is pnpm@9.15.4. Pinned
# by multi-arch index digest (amd64 + arm64) so the base cannot move under a tag.
FROM node:24-trixie-slim@sha256:54f84b4cf8432b21aaffd5d3ce8d41e12cea0d10d33c09d2f3c9879d49d6b446 AS base
ARG USER_UID=1000
ARG USER_GID=1000
RUN apt-get update \
  && apt-get upgrade -y --no-install-recommends \
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

# #2336: upstream's lockfile pins proxy-addr 2.0.7 (Express 5.2.1's tree, parses
# X-Forwarded-For in the served app); 2.0.8 carries the fix and upstream has not
# moved. Force it with a pnpm override in the root package.json, then re-resolve
# ONLY the lockfile (no node_modules) so it agrees with the override; the frozen
# install below then still refuses any other drift. The grep fails the build if
# 2.0.7 survives, e.g. because upstream renamed its override block.
RUN node -e 'const fs=require("fs");const p=JSON.parse(fs.readFileSync("package.json"));p.pnpm=p.pnpm||{};p.pnpm.overrides=Object.assign({},p.pnpm.overrides,{"proxy-addr":"2.0.8"});fs.writeFileSync("package.json",JSON.stringify(p,null,2)+"\n")' \
  && pnpm install --lockfile-only --no-frozen-lockfile \
  && ! grep -qF 'proxy-addr@2.0.7' pnpm-lock.yaml \
  && grep -qF 'proxy-addr@2.0.8' pnpm-lock.yaml

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
  && mkdir -p /paperclip/.paperclip \
  && chown -R node:node /paperclip

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

# #2336: run as the unprivileged node user (uid/gid 1000, home /paperclip). The
# data dir above is created node-owned, so a fresh named volume mounted over it
# inherits that ownership; upstream's docker-entrypoint.sh and our wrapper both
# skip the chown/gosu/usermod steps when they are not root.
USER node

ENTRYPOINT ["entrypoint-wrapper.sh"]
CMD ["node", "--import", "./server/node_modules/tsx/dist/loader.mjs", "server/dist/index.js"]
