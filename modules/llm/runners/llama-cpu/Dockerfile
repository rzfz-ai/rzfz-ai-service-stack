# llama-server CPU runtime — thin wrapper around
# ghcr.io/ggml-org/llama.cpp:server.
#
# Why a wrapper image instead of using ghcr.io/ggml-org/llama.cpp:server
# directly: same reason as llm/runners/llama-rocm/Dockerfile — operators
# want `--flag=value` (one UI row per backend_parameter) but upstream
# llama-server rejects that form. We drop the llama-server-shim onto PATH
# to translate. See llm/runners/llama-server-shim for details.
#
# In the upstream image, llama-server lives at /app/llama-server (the
# image's ENTRYPOINT defaults to it). We add /usr/local/bin/llama-server
# (the shim) and point it at /app/llama-server. We also override
# ENTRYPOINT to the shim so the GPUStack-resolved entrypoint works whether
# it picks up the shim by PATH or by the image's own entrypoint.
#
# Build (context is llm/runners so the shared shim is reachable):
#   docker build -t llama-cpu-runner:b8000 \
#       -f llm/runners/llama-cpu/Dockerfile llm/runners

FROM ghcr.io/ggml-org/llama.cpp:server

COPY llama-server-shim /usr/local/bin/llama-server
RUN chmod +x /usr/local/bin/llama-server
ENV LLAMA_SERVER_BIN=/app/llama-server

# Reset the upstream entrypoint — GPUStack provides its own command.
ENTRYPOINT []

# Sanity check at build time
RUN /app/llama-server --version 2>&1 | head -3 || true
