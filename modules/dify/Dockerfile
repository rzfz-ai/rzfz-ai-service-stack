# SPDX-License-Identifier: Apache-2.0
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# razzfazz-dify-web — CLONE-AT-BUILD of the Dify web frontend (#68 de-vendor)
# =============================================================================
# The Dify web source is no longer vendored in this repo. This Dockerfile clones
# the upstream Dify monorepo at the pinned DIFY_VERSION, optionally applies any
# razzfazz web patch (modules/dify/patches/web-*.patch), and builds the frontend.
# Only this Dockerfile + modules/dify/patches/ + DIFY-LICENSE ship — never the
# 95 MB upstream web tree.
#
# Dify is Apache-2.0 WITH additional conditions (no multi-tenant use; the Dify
# LOGO/copyright must remain in the web image). We build CE single-tenant and do
# NOT remove or modify the logo/copyright. Upstream licence: modules/dify/
# DIFY-LICENSE; attribution: THIRD-PARTY-NOTICES.md.
#
# NB for version bumps: the build stages below mirror the upstream
# web/web/Dockerfile for the 1.15.x line. When bumping DIFY_VERSION across a
# Dify minor that changes its own build recipe (build targets, pnpm steps),
# re-check this file against the upstream web/web/Dockerfile at the new tag.
# 1.14.2 -> 1.15.0 (#26): upstream web/Dockerfile changed only its base pin
# (node:22-alpine -> node:22.22.1-alpine, matched below) AND now builds with
# VITE_GIT_HOOKS=0 because 1.15.0's web workspace adds vite-plus (its `prepare`
# script shells out to git; see base stage). No razzfazz web-*.patch files
# exist, so no patch rebase is needed.
#
# #160 (broken model-provider icons since 1.15.0) is NOT a bug in THIS build.
# Investigated 2026-07 (re-confirmed for 1.16.0). Evidence:
#  1. The production COPY stages below are byte-identical to upstream
#     web/Dockerfile @1.15.0/@1.16.0 — this image == the official dify-web
#     (no de-vendored asset path).
#  2. The provider-list / model-config icons are NOT static web assets. dify-web
#     renders each as a plain `<img src={provider.icon_small.en_US}>` where the
#     value is an ABSOLUTE URL the BACKEND builds:
#       CONSOLE_API_URL/console/api/workspaces/<tenant>/model-providers/
#       <provider>/icon_small/<lang>
#     (services/entities/model_provider_entities.py ProviderResponse validator —
#     byte-identical AND live-verified 1.14.2 -> 1.16.0; the new 1.16
#     ProviderEntityResponse without that validator is UNUSED). dify-api serves
#     the bytes from the plugin-daemon (content-hash assets under
#     /app/storage/assets), and the plugin-daemon RE-EXTRACTS a missing asset
#     from the plugin package on demand (deleting the cache file does NOT break
#     the icon — verified live), so a missing cache file is not the cause.
#  3. The unauthenticated <path:provider> icon route + Caddy's /console/api/*
#     SSO-exemption route the multi-slash provider path correctly — verified live
#     end-to-end (HTTP 200 image through Caddy for openai / gpustack /
#     openai_api_compatible on a running 1.14.2 box; a protected path 302s).
#  4. CSP is not involved (same-origin <img>; our Dify Caddy CSP is report-only;
#     web/proxy.ts only emits img-src when CSP_WHITELIST is non-empty — empty).
#  5. The plugin->daemon asset fetch does NOT traverse the SSRF proxy (the plugin
#     client uses httpx trust_env=False) — so caddy:8195 ACLs are irrelevant.
# So a broken provider icon is a RUNTIME condition of THAT box's dify-api/
# plugin-daemon icon endpoint, not a Dockerfile/web/Caddy/config gap. It is now
# SELF-DIAGNOSING: `rzfz post-install --verify` runs a "Dify: model-provider
# icons render" check (cli/post-install.sh::dify_broken_provider_icons) that
# probes every installed provider's icon endpoint and prints the exact failing
# provider + HTTP status (500 => plugin-daemon/plugin asset; 404 => host/route;
# 302/HTML => SSO — N/A here; stale host => CONSOLE_API_URL). Re-provision /
# `rzfz post-install --refresh` reinstalls the provider plugins, re-extracting
# their icon assets. No Dockerfile/web change fixes it — do NOT add a
# speculative asset COPY.
# =============================================================================
ARG DIFY_VERSION=1.16.0

# ---- clone the pinned upstream source (+ apply any razzfazz web patch) -------
FROM alpine/git AS source
ARG DIFY_VERSION
WORKDIR /src
# /src is the Dify MONOREPO ROOT: pnpm-lock.yaml / pnpm-workspace.yaml /
# package.json live here; the Next.js app is the web/ workspace member, with
# e2e/, sdks/nodejs-client/ and packages/* as sibling members.
RUN git clone --depth 1 --branch "${DIFY_VERSION}" https://github.com/langgenius/dify.git .
COPY patches/ /razzfazz-patches/
# Apply only web-targeted patches (patches/web-*.patch), repo-relative. The
# runtime api/worker patches (entrypoint-wrapper.sh, reset-password-phase.patch)
# are bind-mounted at run time, not baked into the web image.
RUN set -e; \
    for p in /razzfazz-patches/web-*.patch; do \
      [ -e "$p" ] || continue; \
      echo "applying $p"; git -C /src apply "$p"; \
    done; \
    rm -rf /src/.git

# ---- base (mirrors upstream web/Dockerfile) ----------------------------------
# Base pinned to the exact upstream base (was floating node:22-alpine for the
# 1.14.x line; the float had also cached a stale image that masked rebuilds).
# 2026.08 DIFY_VERSION 1.15.0 → 1.16.0 (BASE RE-CHECK — DONE): upstream
#   web/Dockerfile is BYTE-IDENTICAL 1.15.0 → 1.16.0 (base `node:22.22.1-alpine`
#   unchanged; no FROM/ARG/apk/pnpm diff). So this pin stays correct for 1.16.0 —
#   no repin. (Verified by diffing raw web/Dockerfile @1.15.0 vs @1.16.0.) The
#   clone-at-build still needs a real build test, but the base does not drift.
# #26 build fix — VITE_GIT_HOOKS=0: Dify 1.15.0's web workspace adds vite-plus,
# whose `prepare` lifecycle script runs `vp config`, which shells out to `git`.
# Our `source` stage strips .git (rm -rf /src/.git) and this base has no git, so
# `pnpm build`'s implicit dependency re-install would run that prepare script and
# die with "prepare: git command not found". Upstream builds with VITE_GIT_HOOKS=0
# (set on its frozen install) to skip the vite git hook; we set it globally here
# so EVERY pnpm invocation — including the builder's implicit re-install — skips
# it. No git binary is added: there is no .git repo for it to operate on anyway.
FROM node:22.22.1-alpine AS base
LABEL maintainer="razzfazz.ai GmbH"
ENV VITE_GIT_HOOKS=0
RUN apk add --no-cache tzdata && corepack enable
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
ARG NEXT_PUBLIC_BASE_PATH=""
ENV NEXT_PUBLIC_BASE_PATH="$NEXT_PUBLIC_BASE_PATH"
# razzfazz build-time args for runtime URL/cookie config (forwarded by compose)
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_CONSOLE_URL
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_COOKIE_DOMAIN
ARG CONSOLE_API_URL
ARG APP_API_URL
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ENV NEXT_PUBLIC_CONSOLE_URL=${NEXT_PUBLIC_CONSOLE_URL}
ENV NEXT_PUBLIC_APP_URL=${NEXT_PUBLIC_APP_URL}
ENV NEXT_PUBLIC_COOKIE_DOMAIN=${NEXT_PUBLIC_COOKIE_DOMAIN}

# ---- install workspace packages ---------------------------------------------
FROM base AS packages
WORKDIR /app
COPY --from=source /src/package.json /src/pnpm-lock.yaml /src/pnpm-workspace.yaml /app/
COPY --from=source /src/web/package.json /app/web/
COPY --from=source /src/e2e/package.json /app/e2e/
COPY --from=source /src/sdks/nodejs-client/package.json /app/sdks/nodejs-client/
COPY --from=source /src/packages /app/packages
RUN corepack install
RUN VITE_GIT_HOOKS=0 pnpm install --frozen-lockfile

# ---- build ------------------------------------------------------------------
FROM base AS builder
WORKDIR /app
COPY --from=packages /app/ .
# overlay the full monorepo source (no .git) over the installed workspace
COPY --from=source /src/ .
WORKDIR /app/web
ENV NODE_OPTIONS="--max-old-space-size=4096"
RUN pnpm build && pnpm build:vinext

# ---- production -------------------------------------------------------------
FROM base AS production
ENV NODE_ENV=production
ENV EDITION=SELF_HOSTED
ENV DEPLOY_ENV=PRODUCTION
ENV CONSOLE_API_URL=${CONSOLE_API_URL}
ENV APP_API_URL=${APP_API_URL}
ENV MARKETPLACE_API_URL=https://marketplace.dify.ai
ENV MARKETPLACE_URL=https://marketplace.dify.ai
ENV PORT=3000
ENV EXPERIMENTAL_ENABLE_VINEXT=false
ENV NEXT_TELEMETRY_DISABLED=1
ENV TZ=UTC
RUN ln -s /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone
ARG dify_uid=1001
RUN addgroup -S -g ${dify_uid} dify && \
    adduser -S -u ${dify_uid} -G dify -s /bin/ash -h /home/dify dify && \
    mkdir /app && chown -R dify:dify /app
WORKDIR /app
COPY --from=builder --chown=dify:dify /app/web/public ./targets/next/web/public
COPY --from=builder --chown=dify:dify /app/web/.next/standalone ./targets/next/
COPY --from=builder --chown=dify:dify /app/web/.next/static ./targets/next/web/.next/static
COPY --from=builder --chown=dify:dify /app/web/dist/standalone ./targets/vinext
# NB: plain `COPY --chmod` needs BuildKit; a COPY + RUN chmod builds under both
# the classic builder and BuildKit (more portable than upstream's --chmod).
COPY --from=source --chown=dify:dify /src/web/docker/entrypoint.sh ./entrypoint.sh
RUN chmod 755 ./entrypoint.sh
ARG COMMIT_SHA
ENV COMMIT_SHA=${COMMIT_SHA}
USER dify
EXPOSE 3000
ENTRYPOINT ["/bin/sh", "./entrypoint.sh"]
