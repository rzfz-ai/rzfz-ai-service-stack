# llama-server Vulkan runtime for AMD Strix Halo (gfx1151).
#
# Built from upstream llama.cpp release binaries on Ubuntu 24.04, which
# matches the host's Mesa userspace stack (Mesa RADV 25.2.x) and avoids a
# from-source compile in the Docker build. The resulting image is ~600 MB.
#
# Used as a custom backend in GPUStack 2.1.x via the `llama-box-custom`
# pattern: GPUStack runs `docker run llama-vulkan-runner:b8943 \
#   llama-server -m {{model_path}} --host 0.0.0.0 --port {{port}} \
#   -fa 1 --no-mmap -ngl 999 ...`.
#
# Why Vulkan instead of ROCm/HIP on Strix Halo:
# - Mesa RADV's Vulkan compute path on RDNA 3.5 has been hand-tuned for
#   months by AMD/Mesa; ROCm 7.2 for gfx1151 is newer and less mature.
# - M022 stress comparison (.gsd/milestones/M022/RUNTIME-COMPARISON.md):
#   Vulkan closes the code-pool latency gap to the legacy 0.7.1 baseline
#   that the kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1 ROCm runner couldn't.
#
# Build (NOTE: build context is llm/runners, not llm/runners/llama-vulkan,
# so the shared llama-server-shim script is reachable in the context):
#   docker build -t llama-vulkan-runner:b8943 \
#       -f llm/runners/llama-vulkan/Dockerfile llm/runners
#
# To bump llama.cpp version: change LLAMA_CPP_TAG ARG below and rebuild.

FROM ubuntu:24.04

ARG LLAMA_CPP_TAG=b8943
ARG LLAMA_CPP_URL=https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_CPP_TAG}/llama-${LLAMA_CPP_TAG}-bin-ubuntu-vulkan-x64.tar.gz

# Mesa Vulkan + RADV for AMD gfx1151. libgomp for llama.cpp OpenMP.
# tini for proper signal handling. curl + ca-certs for the download step
# (only needed during build; image still uses them for upstream tarball
# fetch — we don't strip them in case operators want to re-pull).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl tini \
      libvulkan1 mesa-vulkan-drivers vulkan-tools \
      libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Download + extract the upstream Vulkan build into /opt/llama
RUN curl -fsSL "${LLAMA_CPP_URL}" -o /tmp/llama.tar.gz \
    && mkdir -p /opt/llama \
    && tar -xzf /tmp/llama.tar.gz -C /opt/llama --strip-components=1 \
    && rm /tmp/llama.tar.gz \
    && ls -la /opt/llama/llama-server /opt/llama/libggml-vulkan.so | head -5 \
    && ldconfig

# Install the llama-server shim as /usr/local/bin/llama-server (first on
# PATH). The shim rewrites GPUStack-style `--flag=value` argv into
# `--flag value` (which upstream llama-server requires) and then execs
# the real binary at $LLAMA_SERVER_BIN. See
# llm/runners/llama-server-shim header for rationale.
COPY llama-server-shim /usr/local/bin/llama-server
RUN chmod +x /usr/local/bin/llama-server
ENV LLAMA_SERVER_BIN=/opt/llama/llama-server

# Make all upstream .so files findable for llama-server
ENV LD_LIBRARY_PATH=/opt/llama:/usr/lib/x86_64-linux-gnu

# Sanity check at build time (no GPU access required for --version)
RUN /opt/llama/llama-server --version 2>&1 | head -3 || true

ENTRYPOINT ["/usr/bin/tini","--"]
