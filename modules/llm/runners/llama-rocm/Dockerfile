# llama-server ROCm runtime for AMD Strix Halo (gfx1151) — thin wrapper
# around kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1.
#
# Why a wrapper image instead of using kyuz0 directly:
# - GPUStack 2.x passes per-model `backend_parameters` verbatim. Operators
#   want `--ctx-size=8192` (one UI row per parameter) but upstream
#   llama-server rejects `--flag=value` and requires `--flag value`. We
#   inject /usr/local/bin/llama-server-shim ahead of the real binary on
#   PATH to translate. See llm/runners/llama-server-shim for details.
# - Keeps the operator's existing run_command template (`llama-server …`)
#   identical across all three runners (vulkan / rocm / cpu); only the
#   image differs.
#
# In the upstream image, llama-server lives at /usr/local/bin/llama-server.
# We MOVE it to /opt/llama/bin/llama-server and drop the shim into its
# place, so anything that resolves `llama-server` via PATH hits the shim.
#
# Build (context is llm/runners so the shared shim is reachable):
#   docker build -t llama-rocm-runner:rocm-7.2.1 \
#       -f llm/runners/llama-rocm/Dockerfile llm/runners

FROM kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1@sha256:f85b391cbed925a1d7cb08e19409155a66612fc6ff7938d42839e6d67f648fb0

# Stash the real binary out of PATH then drop the shim in its place.
RUN mkdir -p /opt/llama/bin \
    && mv /usr/local/bin/llama-server /opt/llama/bin/llama-server

COPY llama-server-shim /usr/local/bin/llama-server
RUN chmod +x /usr/local/bin/llama-server
ENV LLAMA_SERVER_BIN=/opt/llama/bin/llama-server

# Sanity check at build time (no GPU access required for --version)
RUN /opt/llama/bin/llama-server --version 2>&1 | head -3 || true
