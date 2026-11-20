# ==============================================================================
# coding-tools image
# Combines gsd-pi (pi AI coding agent) and opencode (AI code editor CLI)
# into a single container with a shared Gitea-configured workspace.
#
# Base: node:24-bookworm-slim
# Installs: gsd-pi (gsd CLI), opencode-ai (opencode CLI)
#           Python3 + Flask web UI with xterm.js WebSocket terminal on :3004
# Workspace: /workspace (persisted volume, shared between agents)
# User: agent (uid 1000) — not root
# ==============================================================================

FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c

# System deps
RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    procps \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user — reuse the existing 'node' user (uid 1000) from the base image,
# just rename it to 'agent' for clarity, and set its home dir
RUN usermod -l agent -d /home/agent -m node \
    && groupmod -n agent node

# Python web UI deps (installed as root, runs as agent)
RUN python3 -m venv /opt/webui-venv \
    && /opt/webui-venv/bin/pip install --no-cache-dir \
        flask \
        flask-sock \
        ptyprocess

# Install AI coding tools globally — pinned for reproducibility (M023-S02).
# Bump via the check-and-bump-versions skill — never re-pin to "latest".
# NOTE: this is the SUPERSEDED bundled coding-tools image (catalog enabled=False,
# replaced by the per-type split in coding-agent-web/). Kept buildable for any
# still-provisioned legacy instance. opencode aligned 1.17.5 → 1.17.13 to match
# the running image + the split builder. gsd stays on the OLD deprecated `gsd-pi`
# package here (3.0.0, its last release); the maintained @opengsd/gsd-pi is used
# only by the split builder.
ARG GSD_PI_VERSION=3.0.0
ARG OPENCODE_VERSION=1.18.32
RUN npm install -g "gsd-pi@${GSD_PI_VERSION}" "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

# Workspace directory
RUN mkdir -p /workspace && chown agent:agent /workspace

# Home dirs for persisted volumes — created here so Docker volume mounts
# inherit the correct ownership on first run
RUN mkdir -p /home/agent/.gsd \
             /home/agent/.config/opencode \
             /home/agent/.local/share/opencode \
             /home/agent/.local/state/opencode \
             /home/agent/.cache/opencode \
    && chown -R agent:agent /home/agent

WORKDIR /workspace

# Web UI
COPY web/ /opt/coding-tools-web/
RUN chown -R agent:agent /opt/coding-tools-web

EXPOSE 3004

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

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

USER agent
ENTRYPOINT ["/entrypoint.sh"]
