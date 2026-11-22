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
# (the shim) and point it at /app/llama-server, so the name `llama-server`
# resolves to the shim through PATH.
#
# #2017: this paragraph used to say we ALSO override ENTRYPOINT to the shim.
# We do not, and must not — see the ENTRYPOINT line below. The two statements
# contradicted each other for as long as nothing depended on either.
#
# Build (context is llm/runners so the shared shim is reachable):
#   docker build -t llama-runner:b12110-cpu \
#       -f llm/runners/llama-cpu/Dockerfile llm/runners

# rzfz-os-upgrade-exempt: digest-pinned upstream engine image (#1309, #1782). The digest IS the measured engine build (b12110 = llama.cpp v0.9.0: image label revision cd959bae99ff, version b12110, tag server-b12110, measured 2026-11-08); re-pinning is an engine bump with its own CPU acceptance run, not an OS refresh, so no apt upgrade runs on top of it.
FROM ghcr.io/ggml-org/llama.cpp:server@sha256:1145e8de3111dc9358c6d8daffb755c4cf8a862d93391c26ba521b9a810bbe3e

COPY llama-server-shim /usr/local/bin/llama-server
RUN chmod +x /usr/local/bin/llama-server
ENV LLAMA_SERVER_BIN=/app/llama-server

# LOAD-BEARING since #2017 — do not restore an entrypoint here.
#
# The caller is the node-agent, not GPUStack (that stopped being true at the
# #1447 cutover; the old comment here still said otherwise). The agent supplies
# the COMPLETE argv, program included: drivers/cpu.py passes
# binary="llama-server", which resolves to the shim copied above via PATH.
#
# So an entrypoint here would be prepended to an argv that already starts with
# the program — `llama-server llama-server --model …`. Before #2017 the driver
# passed no binary, this empty entrypoint left the container with NO program at
# all, and every engine died on `exec: "--model"`. Pinned by
# tests/unit/llm-node-agent/test_2017_every_llamacpp_driver_leads_with_the_binary.py.
ENTRYPOINT []

# Sanity check at build time
RUN /app/llama-server --version 2>&1 | head -3 || true
