# ==============================================================================
# moltis — built from upstream source (rc6.7 #56)
#
# Why we build instead of pulling ghcr.io/moltis-org/moltis:
# the GHCR org slug returns 404 for anonymous pulls (no public package
# published under that name), but the source tree at
# https://github.com/moltis-org/moltis is public and ships dated release
# tags (YYYYMMDD.NN). Building locally avoids any dependency on a private
# GHCR package and matches the pattern coding-tools / paperclip use.
#
# This Dockerfile re-implements the upstream Dockerfile inline (via a
# clone-then-build pattern) so the only required input is a git tag. If
# upstream changes their build, bump MOLTIS_VERSION and adjust the RUN
# steps below to match. Upstream Dockerfile reference:
# https://github.com/moltis-org/moltis/blob/<TAG>/Dockerfile
#
# Image is tagged razzfazz-stack-moltis:<MOLTIS_VERSION>; the agent-manager
# catalog references this name + tag.
#
# Note: the build is heavy (Rust release build of a workspace with
# llama-cpp-sys, plus WASM components and a Vite/Tailwind web asset
# pipeline). Expect 10-30 minutes on first build; subsequent rebuilds are
# fully cached on the rust target/ dir.
# ==============================================================================

# Global ARG (declared before the first FROM) so EVERY stage inherits the pinned
# default; compose/.env override it. A stage-local default does not reach the
# builder stage, which used to bake an EMPTY MOLTIS_VERSION on a bare build.
ARG MOLTIS_VERSION=20261023.01

# --- clone upstream source at the pinned tag ---
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3 AS clone
ARG MOLTIS_VERSION
RUN test -n "${MOLTIS_VERSION}" || { echo "ERROR: MOLTIS_VERSION is empty; refusing to clone a floating ref" >&2; exit 1; }
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${MOLTIS_VERSION} \
        https://github.com/moltis-org/moltis /src && \
    rm -rf /src/.git

# --- build stage (rust nightly + cmake + node) ---
# NodeSource apt signing key, pinned by fingerprint (NodeSource <nsolid-gpg@nodesource.com>,
# primary key 6F71F525282841EEDAF851B42F59B5F99B1BE0B4, checked 2026-10-18).
# The key is DATA: fetched, its fingerprint compared, then dearmored into a keyring; apt verifies every nodejs package against it.
# Replaces `curl .../setup_22.x | bash -` (unreviewable remote script run as root).
FROM rust:bookworm@sha256:9181b56c2a21ab1d5e73cffe04fc762426d30aa7fac3c91141dd033f49a7fc8b AS builder
WORKDIR /build

# Pull just what we need to discover the nightly pin first
COPY --from=clone /src/rust-toolchain.toml ./
RUN NIGHTLY="$(sed -nE 's/^channel[[:space:]]*=[[:space:]]*"([^"]+)"/\1/p' rust-toolchain.toml)" \
    && rustup install "$NIGHTLY" && rustup default "$NIGHTLY"

# Copy the FULL cloned source rather than selective subdirs. As of moltis
# 20260603.01 crates/agents bundles docs/ into the binary at compile time via
# include_dir!("$CARGO_MANIFEST_DIR/../../docs/src"), and docs/src carries
# symlinks into the repo root (changelog.md -> ../../CHANGELOG.md). A selective
# COPY breaks on each such cross-reference; a full copy mirrors upstream's own
# Dockerfile and is robust against future bundle additions.
COPY --from=clone /src ./

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && \
    apt-get install -yqq --no-install-recommends \
        cmake build-essential libclang-dev pkg-config git \
        ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key -o /tmp/nodesource.key && \
    fpr="$(gpg --show-keys --with-colons /tmp/nodesource.key | awk -F: '/^fpr:/{print $10; exit}')" && \
    [ "$fpr" = "6F71F525282841EEDAF851B42F59B5F99B1BE0B4" ] || { echo "ERROR: NodeSource key fingerprint mismatch: got $fpr" >&2; exit 1; } && \
    gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg < /tmp/nodesource.key && rm -f /tmp/nodesource.key && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update -qq && \
    apt-get install -yqq --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Web assets (Vite + Tailwind + service worker)
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)  TW="tailwindcss-linux-x64" ;; \
        aarch64) TW="tailwindcss-linux-arm64" ;; \
    esac && \
    curl -sLO "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/$TW" && \
    chmod +x "$TW" && \
    TAILWINDCSS="./$TW" ./scripts/build-web-assets.sh

# WASM components (embedded into the binary via include_bytes!)
# #2560: the caller's per-build job bound (razzfazz_compose_build). Declared right
# before each heavy RUN so the value keys only those layers' cache. Empty =
# cargo's own default (a build outside the rzfz tooling).
ARG RAZZFAZZ_BUILD_JOBS=
RUN if [ -z "$RAZZFAZZ_BUILD_JOBS" ]; then n=$(nproc); m=$(awk '/^MemAvailable:/{printf "%d", $2/2097152}' /proc/meminfo 2>/dev/null); m=${m:-1}; [ "$m" -ge 1 ] || m=1; if [ "$m" -lt "$n" ]; then n=$m; fi; RAZZFAZZ_BUILD_JOBS=$n; echo "RAZZFAZZ_BUILD_JOBS empty: derived CARGO_BUILD_JOBS=$n (min of cores and MemAvailable/2 GB, #2747)"; fi; if [ -n "$RAZZFAZZ_BUILD_JOBS" ]; then export CARGO_BUILD_JOBS="$RAZZFAZZ_BUILD_JOBS"; fi; \
    rustup target add wasm32-wasip2 && \
    cargo build --target wasm32-wasip2 \
        -p moltis-wasm-calc \
        -p moltis-wasm-web-fetch \
        -p moltis-wasm-web-search --release

ARG MOLTIS_VERSION
ENV MOLTIS_VERSION=${MOLTIS_VERSION}
# #2560: see the WASM step; CARGO_BUILD_JOBS for rustc, MAKEFLAGS/CMAKE for the
# C/C++ build scripts (-sys crates) cargo runs.
ARG RAZZFAZZ_BUILD_JOBS=
RUN if [ -z "$RAZZFAZZ_BUILD_JOBS" ]; then n=$(nproc); m=$(awk '/^MemAvailable:/{printf "%d", $2/2097152}' /proc/meminfo 2>/dev/null); m=${m:-1}; [ "$m" -ge 1 ] || m=1; if [ "$m" -lt "$n" ]; then n=$m; fi; RAZZFAZZ_BUILD_JOBS=$n; echo "RAZZFAZZ_BUILD_JOBS empty: derived CARGO_BUILD_JOBS=$n (min of cores and MemAvailable/2 GB, #2747)"; fi; if [ -n "$RAZZFAZZ_BUILD_JOBS" ]; then export CARGO_BUILD_JOBS="$RAZZFAZZ_BUILD_JOBS" MAKEFLAGS="-j$RAZZFAZZ_BUILD_JOBS" CMAKE_BUILD_PARALLEL_LEVEL="$RAZZFAZZ_BUILD_JOBS"; fi; ./scripts/cargo-build-moltis.sh --release

# --- runtime stage (mirrors upstream Dockerfile) ---
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
ENV DEBIAN_FRONTEND=noninteractive

# ── Extra system (apt) packages baked at BUILD time (#212) ────────────────────
# See modules/agents/coding-agent-web/Dockerfile for the full rationale: per-user
# agents run NON-ROOT, so `apt install` only works at build time. (Like the
# coding agents, this image has no sudo since #2665.) This knob bakes
# extra libs without hand-editing this Dockerfile — append names to USER_DEFINED_APT_PACKAGES in .env, rebuild
# (rzfz post-install --refresh), re-provision. Default = the Playwright/Chromium
# runtime lib set (bookworm names). NOTE: moltis already installs the full
# `chromium` package below, so these libs are typically already satisfied; the
# default keeps the knob consistent across all agent images and is a no-op when
# the deps are already present.
ARG USER_DEFINED_APT_PACKAGES="libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 libx11-6 libxcb1 libxext6 libxi6 fonts-liberation"

# #2336: the agent's browser renders UNTRUSTED web pages, so Chromium must be a
# build from the current Debian security stream, not whatever the cached base
# layer shipped. `upgrade` brings every package of the base (perl-base, libexpat,
# ...) to today's security level, and the check below refuses to produce an image
# whose Chromium is older than the fixed major (bookworm-security: 155.0.8059.79).
#
# Chromium sandbox: upstream moltis hard-codes `--no-sandbox` and
# `--disable-setuid-sandbox` in its host-browser launch
# (crates/browser/src/pool.rs, at the pinned tag), so the browser runs without its
# own sandbox. Dropping the flags is not possible from here: Chromium's sandbox
# needs user namespaces, which the default seccomp profile and the agent
# containers' cap_drop ALL + no-new-privileges (#2664) deny. The CONTAINER is the
# sandbox: non-root user, no docker socket, read-only rootfs, dropped
# capabilities, per-user network. Revisit when upstream makes the flag optional.
# No editor in the image (#2336): vim-tiny and its vim-common dependency carried
# about 46 of the High findings, none with a published fix, and the agent has no
# use for an interactive editor (it edits through its tools). The check inside
# the RUN below fails the build if either package comes back, e.g. as a
# dependency of something added later.
ARG CHROMIUM_MIN_MAJOR=155
RUN apt-get update -qq && \
    apt-get -yqq upgrade && \
    apt-get install -yqq --no-install-recommends \
        ca-certificates chromium curl gnupg libgomp1 tmux && \
    for p in vim-tiny vim-common; do \
        if dpkg -s "$p" >/dev/null 2>&1; then echo "ERROR: $p is installed; the image ships no editor (#2336)" >&2; exit 1; fi; \
    done && \
    v="$(dpkg-query -W -f='${Version}' chromium)" && \
    if ! dpkg --compare-versions "$v" ge "${CHROMIUM_MIN_MAJOR}.0"; then \
        echo "ERROR: chromium $v is older than the fixed major ${CHROMIUM_MIN_MAJOR} (#2336)" >&2; exit 1; \
    fi && \
    echo "chromium $v" && \
    rm -rf /var/lib/apt/lists/*

# Node.js 22 (for stdio MCP servers)
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key -o /tmp/nodesource.key && \
    fpr="$(gpg --show-keys --with-colons /tmp/nodesource.key | awk -F: '/^fpr:/{print $10; exit}')" && \
    [ "$fpr" = "6F71F525282841EEDAF851B42F59B5F99B1BE0B4" ] || { echo "ERROR: NodeSource key fingerprint mismatch: got $fpr" >&2; exit 1; } && \
    gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg < /tmp/nodesource.key && rm -f /tmp/nodesource.key && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update -qq && \
    apt-get install -yqq --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# No Docker CLI, no docker group, no sudo (#2665): the container runs with no
# mounted socket (#2661), so none of them has a use left; each would matter again
# the day a socket or a privilege-gaining exec path came back.
RUN useradd --create-home --user-group moltis

COPY --from=builder /build/target/release/moltis /usr/local/bin/moltis
# zvec runtime (#1246). `moltis` is built with the `full` feature set (see
# scripts/cargo-build-moltis.sh, MOLTIS_BUILD_FEATURES defaults to `full`), and
# upstream's own crates/cli/Cargo.toml says `full` "adds zvec for source and
# container builds, where its native runtime can be provided". zvec-rust-sys
# 0.6.0 links that runtime DYNAMICALLY — its build.rs ends with
# `cargo:rustc-link-lib=dylib=zvec_c_api` and offers no static feature — and
# obtains the library in its build-script OUT_DIR (a prebuilt .so downloaded
# from the zvec-ai/zvec-rust release, extracted to $OUT_DIR/zvec-prebuilt). The
# same build.rs also emits `-Wl,-rpath,<that OUT_DIR>`, which is why the binary
# runs fine in the builder stage and then exits 127 here: without this COPY the
# rpath points into a `target/` dir that does not exist in the runtime image
# and `ldd` reports `libzvec_c_api.so => not found`.
# The <hash> in the path changes with features/rustc, hence the glob. This
# mirrors upstream's Dockerfile line 115 verbatim; keep it in sync on bumps.
COPY --from=builder /build/target/release/build/zvec-rust-sys-*/out/zvec-prebuilt/libzvec_c_api.so /usr/local/lib/
COPY --from=builder /build/crates/web/src/assets /usr/share/moltis/web
COPY --from=builder /build/target/wasm32-wasip2/release/moltis_wasm_calc.wasm /usr/share/moltis/wasm/
COPY --from=builder /build/target/wasm32-wasip2/release/moltis_wasm_web_fetch.wasm /usr/share/moltis/wasm/
COPY --from=builder /build/target/wasm32-wasip2/release/moltis_wasm_web_search.wasm /usr/share/moltis/wasm/

# `ldconfig` publishes the .so above into the loader cache (/usr/local/lib is on
# Debian's default search path via /etc/ld.so.conf.d/libc.conf), and
# `moltis --version` is the BUILD-TIME GATE: it is the very command the
# moltis-image-builder one-shot runs, so a missing or unusable shared library
# now fails this build instead of shipping an image that cannot start (#1246).
# Also upstream's Dockerfile line 120.
RUN ldconfig && moltis --version

RUN mkdir -p /home/moltis/.config/moltis /home/moltis/.moltis /home/moltis/.npm && \
    chown -R moltis:moltis /home/moltis/.config /home/moltis/.moltis /home/moltis/.npm

# Bake extra system (apt) packages while still root — see USER_DEFINED_APT_PACKAGES
# ARG above. Runs AFTER all image apt work and BEFORE the drop to `moltis`, so the
# runtime never needs apt: the `moltis` user has no sudo.
RUN if [ -n "${USER_DEFINED_APT_PACKAGES}" ]; then \
        apt-get update -qq && apt-get install -yqq --no-install-recommends ${USER_DEFINED_APT_PACKAGES} \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# #2476: the OpenBao CLI, pinned and checksum-verified. The agent reads its
# owner's short-lived, sub-keyed token from /run/rzfz/bao-token (a 0700 tmpfs).
ARG OPENBAO_CLI_VERSION=2.6.3
ARG OPENBAO_CLI_SHA256_AMD64=c6463ddd4fdc4214b62a7ffdeaa0fc6df170f7e6b75c6dea2c5e525bdc932ed3
ARG OPENBAO_CLI_SHA256_ARM64=47858610a9853b15e2541c69c0a884861b881e2f4a172fccc429ce5cd3f047bd
ARG TARGETARCH
RUN set -eu; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
      amd64) sum="$OPENBAO_CLI_SHA256_AMD64" ;; \
      arm64) sum="$OPENBAO_CLI_SHA256_ARM64" ;; \
      *) echo "no pinned OpenBao CLI for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /tmp/openbao.tgz \
      "https://github.com/openbao/openbao/releases/download/v${OPENBAO_CLI_VERSION}/openbao_${OPENBAO_CLI_VERSION}_linux_${arch}.tar.gz"; \
    echo "$sum  /tmp/openbao.tgz" | sha256sum -c -; \
    tar -xzf /tmp/openbao.tgz -C /usr/local/bin bao; \
    rm -f /tmp/openbao.tgz; \
    chmod 0755 /usr/local/bin/bao; \
    bao version
# #2476: rzfz-secret, the bao CLI with the agent owner's token. OpenBao is
# reached through the Agent Manager's read-only proxy, never directly.
RUN printf '%s\n' '#!/bin/sh' 'set -eu' 'tok=$(cat /run/rzfz/bao-token 2>/dev/null) || tok=""' 'if [ -z "$tok" ]; then echo "rzfz-secret: no OpenBao token in /run/rzfz (OpenBao is off, or this agent runs as a technical user)" >&2; exit 1; fi' 'BAO_TOKEN=$tok; export BAO_TOKEN' 'BAO_ADDR=${BAO_ADDR:-http://agent-manager:5000/bao}; export BAO_ADDR' 'NO_PROXY=${NO_PROXY:+$NO_PROXY,}agent-manager; no_proxy=${no_proxy:+$no_proxy,}agent-manager; export NO_PROXY no_proxy' 'exec bao "$@"' > /usr/local/bin/rzfz-secret && chmod 0755 /usr/local/bin/rzfz-secret

VOLUME ["/home/moltis/.config/moltis", "/home/moltis/.moltis", "/home/moltis/.npm"]

USER moltis
WORKDIR /home/moltis

EXPOSE 13131 13132 1455
ENTRYPOINT ["moltis"]
CMD ["--bind", "0.0.0.0", "--port", "13131"]
