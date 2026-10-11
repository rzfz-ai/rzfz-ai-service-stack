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

# --- clone upstream source at the pinned tag ---
FROM debian:bookworm-slim AS clone
ARG MOLTIS_VERSION=20260827.01
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${MOLTIS_VERSION} \
        https://github.com/moltis-org/moltis /src && \
    rm -rf /src/.git

# --- build stage (rust nightly + cmake + node) ---
FROM rust:bookworm AS builder
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
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
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
RUN rustup target add wasm32-wasip2 && \
    cargo build --target wasm32-wasip2 \
        -p moltis-wasm-calc \
        -p moltis-wasm-web-fetch \
        -p moltis-wasm-web-search --release

ARG MOLTIS_VERSION
ENV MOLTIS_VERSION=${MOLTIS_VERSION}
RUN ./scripts/cargo-build-moltis.sh --release

# --- runtime stage (mirrors upstream Dockerfile) ---
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive

# ── Extra system (apt) packages baked at BUILD time (#212) ────────────────────
# See modules/agents/coding-agent-web/Dockerfile for the full rationale: per-user
# agents run NON-ROOT on a READ-ONLY root fs with NO sudo, so `apt install` only
# works at build time. This knob bakes extra libs without hand-editing this
# Dockerfile — append names to USER_DEFINED_APT_PACKAGES in .env, rebuild
# (rzfz post-install --refresh), re-provision. Default = the Playwright/Chromium
# runtime lib set (bookworm names). NOTE: moltis already installs the full
# `chromium` package below, so these libs are typically already satisfied; the
# default keeps the knob consistent across all agent images and is a no-op when
# the deps are already present.
ARG USER_DEFINED_APT_PACKAGES="libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 libx11-6 libxcb1 libxext6 libxi6 fonts-liberation"

RUN apt-get update -qq && \
    apt-get install -yqq --no-install-recommends \
        ca-certificates chromium curl gnupg libgomp1 sudo tmux vim-tiny && \
    rm -rf /var/lib/apt/lists/*

# Node.js 22 (for stdio MCP servers)
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update -qq && \
    apt-get install -yqq --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Docker CLI (for sandbox exec; daemon is the mounted host socket)
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update -qq && \
    apt-get install -yqq --no-install-recommends \
        docker-buildx-plugin docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -f docker && \
    useradd --create-home --user-group moltis && \
    usermod -aG docker moltis && \
    echo "moltis ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/moltis

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
# running sandbox stays hardened (build-time root only; no runtime apt/sudo).
RUN if [ -n "${USER_DEFINED_APT_PACKAGES}" ]; then \
        apt-get update -qq && apt-get install -yqq --no-install-recommends ${USER_DEFINED_APT_PACKAGES} \
        && rm -rf /var/lib/apt/lists/*; \
    fi

VOLUME ["/home/moltis/.config/moltis", "/home/moltis/.moltis", "/home/moltis/.npm", "/var/run/docker.sock"]

USER moltis
WORKDIR /home/moltis

EXPOSE 13131 13132 1455
ENTRYPOINT ["moltis"]
CMD ["--bind", "0.0.0.0", "--port", "13131"]
