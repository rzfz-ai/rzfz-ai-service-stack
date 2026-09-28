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

FROM node:24-bookworm-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
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
ARG OPENCODE_VERSION=1.18.25
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

USER agent
ENTRYPOINT ["/entrypoint.sh"]
