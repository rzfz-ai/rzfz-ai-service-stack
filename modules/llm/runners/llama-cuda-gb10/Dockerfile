# llama-server CUDA runtime for the NVIDIA GB10 (Grace Blackwell, sm_121; DGX Spark /
# HP ZGX Nano class). Sibling of ../llama-cuda/Dockerfile (RTX PRO 6000, sm_120).
#
# The LLM-Manager worker-agent launches this DIRECTLY (CudaLlamaCppDriver) — no
# GPUStack. GPUStack ran llama.cpp via its bundled `llama-box`; this is the same
# llama.cpp `llama-server` (the good, fast MoE engine on this hardware) packaged
# as a standalone razzfazz runner the manager can start on its own, mirroring the
# AMD Vulkan runner (llm/runners/llama-vulkan).
#
# Why build from source (unlike the Vulkan/CPU runners which pull an upstream
# tarball): upstream llama.cpp ships NO prebuilt CUDA binaries for Blackwell
# sm_120. THIS file is the GB10 variant (see the block above the builder stage):
# same b9851, compiled with -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121a-real.
# This is the exact build that was proven inside the GPUStack image
# (~/bw-vision-build), lifted out of GPUStack into a first-class runner.
#
# Build (context is llm/runners so the shared shim is reachable):
#   docker build -t llama-runner:b9851-cuda13.0-sm121a \
#       -f modules/llm/runners/llama-cuda-gb10/Dockerfile modules/llm/runners
#
# Host prerequisites: NVIDIA driver >= 575/580 (CUDA 12.8/12.9 runtime) + the
# nvidia container runtime (the driver passes runtime=nvidia + NVIDIA_VISIBLE_
# DEVICES). To bump llama.cpp: change LLAMA_CPP_TAG and re-run the DevBox
# acceptance test (acceptance + MTP on/off output diff), not casually.

# ── builder: compile llama.cpp b9851 for CUDA sm_121a (GB10) ─────────────────
# NODE-11 (a): the base images are pinned by DIGEST. A tag alone is mutable —
# nvidia rebuilds `13.0.2-devel-ubuntu24.04` under the same name, so two builds of this file months
# apart were not the same image. The tag is kept in FRONT of the digest so the
# line still says what it is; what docker resolves is the digest.
#
# Resolved 2026-09-08 against registry-1.docker.io. Both are multi-arch INDEX
# digests (amd64 + arm64), so pinning them loses no platform — which matters
# here: the GB10 sibling of this pair is arm64.
#
# To bump, re-resolve BOTH and change them in the same commit:
#   docker buildx imagetools inspect nvidia/cuda:<tag>
# The note that stood here said they "cannot be resolved from this repo". That
# was true of a build host without egress, not of the values.
# ==============================================================================
# GB10 variant (#1329 / #1332) — llama-cuda-gb10-runner
# ==============================================================================
# The GB10 (Grace Blackwell, DGX Spark / HP ZGX Nano class) is sm_121, and CUDA
# 12.8's nvcc does not know sm_121 at all. Operator decision 2026-09-04: two
# tags, not one fat binary — `llama-cuda-runner` (../llama-cuda/Dockerfile) stays
# CUDA 12.8 / sm_120 for the RTX PRO 6000, byte-identical to the build measured
# on the Exoscale box; this file differs from it in EXACTLY three build lines:
#   * both FROM lines: nvidia/cuda:13.0.2-{devel,runtime}-ubuntu24.04
#   * -DCMAKE_CUDA_ARCHITECTURES=121a-real  (the `a` suffix enables the
#     architecture-specific features, incl. native NVFP4 — the box's whole
#     performance argument; `-real` = SASS only, no PTX, so the image is GB10-only
#     by design)
# tests/unit/llm-node-agent/test_1329_gb10_runner_dockerfile.py pins that diff.
#
# Measured on the first GB10 box (zgx-684f, DGX OS, driver 580.173.02,
# 2026-09-05, #1332 Phase 0 M4): builds natively on arm64 in 255 s (20 cores),
# image 2.62 GB, serves Qwen3-0.6B-Q8_0 at 262 tok/s with no kernel-image or
# PTX/JIT messages. The sm_120 image (M2) does NOT run there: "no kernel image
# is available for execution on the device". llama.cpp #18022 (libcuda.so.1 at
# link time) did not bite — the stub symlinks below cover CUDA 13 too.
#
# Where it is built: NOT on the master. A GB10 is a thin worker (never a master)
# and arm64; an amd64 master cannot cross-build a CUDA image in useful time. The
# node builds it (#1332 Phase 2 / #1438 build-on-node) — this file is the recipe.
# Build context is the parent directory (the shim is copied from there), exactly
# like the sm_120 runner:
#   docker build -t llama-cuda-gb10-runner:b9851 \
#       -f modules/llm/runners/llama-cuda-gb10/Dockerfile modules/llm/runners
# ==============================================================================
FROM nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:5dc1bca23d05bd37b011be68ec470c03b403a5da07ec3a86e41af9470e9d0cc6 AS builder
ENV DEBIAN_FRONTEND=noninteractive
ARG LLAMA_CPP_TAG=b9851
# NODE-11 (c): the tag above is a MUTABLE git ref, so the commit it pointed at is
# recorded here and asserted by the build below. Resolved 2026-09-08:
#   git ls-remote https://github.com/ggml-org/llama.cpp refs/tags/b9851
#     -> 0eca4d490e591d4e93058d07540cf47278a72577
# Bump it in the SAME commit as LLAMA_CPP_TAG. An empty value still builds, with
# a warning, exactly as before.
ARG LLAMA_CPP_COMMIT="0eca4d490e591d4e93058d07540cf47278a72577"
# nodejs (nodesource 20.x) is required: the b9851 llama-server target embeds the
# web-UI assets, whose build invokes node — without it the `llama-ui-assets`
# target fails and llama-server never links.
#
# NODE-11 (b): this used to be `curl … /setup_20.x | bash -`, the ONE build step
# in this tree that piped an unpinned remote script into a root shell — whatever
# nodesource serves at build time executes with full privileges, and neither the
# script nor its changes are reviewable from here. The sibling AMD/Vulkan runner
# deliberately fetches a tarball instead. Registering the apt repository by hand
# does exactly what that script does for us, with no remote code execution: the
# GPG key is DATA (dearmored into a keyring, never run), and apt then verifies
# every package signature against it.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git patchelf libcurl4-openssl-dev ca-certificates curl gnupg \
  && install -d -m 0755 /usr/share/keyrings \
  && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg \
  && chmod 0644 /usr/share/keyrings/nodesource.gpg \
  && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
  && apt-get update && apt-get install -y --no-install-recommends nodejs \
  && node --version \
  && rm -rf /var/lib/apt/lists/*
RUN git clone --branch ${LLAMA_CPP_TAG} --depth 1 https://github.com/ggml-org/llama.cpp.git /src \
  && if [ -n "${LLAMA_CPP_COMMIT}" ]; then \
       got="$(git -C /src rev-parse HEAD)"; \
       test "$got" = "${LLAMA_CPP_COMMIT}" \
         || { echo "llama.cpp tag ${LLAMA_CPP_TAG} moved: expected ${LLAMA_CPP_COMMIT}, got $got" >&2; exit 1; }; \
     else \
       echo "WARNING: LLAMA_CPP_COMMIT unset — building whatever the MUTABLE tag ${LLAMA_CPP_TAG} points at today (NODE-11)" >&2; \
     fi
# Null-valued optional OAI params (frequency_penalty/top_p/... = null) must be
# treated as "not provided" (use default) — OpenAI-compatible clients (Dify's
# openai_api_compatible plugin, GPUStack Playground) send these as null and
# b9851 otherwise 400s. Skip null items in both OAI copy loops.
RUN perl -pi -e 's/(for \(const auto & item : body\.items\(\)\) \{)/$1\n            if (item.value().is_null()) { continue; }/' /src/tools/server/server-common.cpp \
  && test "$(grep -c "item.value().is_null()" /src/tools/server/server-common.cpp)" = "2"
WORKDIR /src
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 \
  && cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121a-real -DGGML_RPC=ON \
      -DBUILD_SHARED_LIBS=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_CURL=ON \
      -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/lib64/stubs -Wl,--allow-shlib-undefined" \
      -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,--allow-shlib-undefined" \
  && cmake --build build -j"$(nproc)" -t llama-server
# NODE-11 (d): the `|| true` that used to close this line swallowed the failure
# of BOTH the shared-library copy and every patchelf — a build that produced no
# .so, or a binary whose rpath was never set, still "succeeded" and only failed
# at engine start with a loader error. -DBUILD_SHARED_LIBS=ON guarantees the
# libraries exist, so the copy is now a hard step; the per-file patchelf stays
# tolerant (the loop legitimately walks non-ELF files) but the ONE result that
# matters is asserted afterwards.
RUN mkdir -p /out && cp -L build/bin/llama-server /out/ \
  && cp -a build/bin/*.so* /out/ \
  && cd /out && for f in *; do patchelf --set-rpath '$ORIGIN' "$f" 2>/dev/null || true; done \
  && patchelf --print-rpath /out/llama-server | grep -q '\$ORIGIN' \
  && ls -la /out

# ── runtime: CUDA runtime + the compiled llama-server + the arg-shim ─────────
# NODE-11 (b): tag-pinned, same TODO as the builder stage above.
FROM nvidia/cuda:13.0.2-runtime-ubuntu24.04@sha256:6a0e31b59e70890446f2c17356d6efc0d54260090a8c63d6ca7c2ad049db95d2
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates tini libgomp1 libcurl4 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /out/ /opt/llama/
RUN ldconfig

# Same shim as the other runners: rewrites GPUStack-style `--flag=value` into
# `--flag value` (upstream llama-server requires the space form) then execs the
# real binary at $LLAMA_SERVER_BIN. The worker-agent already emits the space form,
# so the shim is a harmless compatibility layer shared across all runners.
COPY llama-server-shim /usr/local/bin/llama-server
RUN chmod +x /usr/local/bin/llama-server
ENV LLAMA_SERVER_BIN=/opt/llama/llama-server
ENV LD_LIBRARY_PATH=/opt/llama:/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu

# Sanity check at build time (no GPU access required for --version).
# NODE-11 (d): the trailing `|| true` is gone — it was pure noise, since the exit
# status of a pipeline is `head`'s and already zero whatever llama-server does.
RUN /opt/llama/llama-server --version 2>&1 | head -3

# NODE-11 (d, deferred): no USER here, so llama-server runs as root inside the
# engine container — with the nvidia driver capabilities attached. Adding a
# non-root USER means pairing it with RAZZFAZZ_GPU_GROUP_GIDS (see
# app/drivers/amd.py::_gpu_group_add) and re-checking /dev/nvidia* permissions
# and models-volume readability on a real Blackwell box; it cannot be validated
# off-box and is NOT part of this change. Tracked in #1037.
ENTRYPOINT ["/usr/bin/tini","--"]
