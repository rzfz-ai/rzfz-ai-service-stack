#!/bin/bash
# Build the patched llama.cpp Vulkan binaries from source.
#
# Produces in this dir:
#   llama-server                 (the patched binary)
#   lib*.so / lib*.so.0 / lib*.so.X.Y.Z   (shared libs + symlinks)
#
# Then ./deploy.sh installs them into a running gpustack container.
#
# Build details:
#   - base: llama.cpp@b9112 (newer than gpustack's stock b9101 so PR #22458
#     applies cleanly; older than b9120+ which uses cooperative-matrix-2
#     Vulkan extensions Ubuntu 24.04's stock libvulkan-dev doesn't have)
#   - patch: PR #22458 — allow NULL tensor->data in set/get_tensor for
#     device-only buffers. Fixes the gemma4 SWA prompt-cache crash on
#     Strix Halo Vulkan.
#   - container: ubuntu:24.04 + libvulkan-dev + glslc + spirv-headers
#   - rpath: $ORIGIN (set via patchelf so libs resolve from install dir)
#
# Idempotent: re-running rebuilds. Output dir cleaned at start.

set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH="$SELF_DIR/22458.patch"
LLAMA_TAG="b9112"

if [ ! -f "$PATCH" ]; then
    echo "ERROR: $PATCH not found"
    exit 1
fi

echo "==> cleaning prior build artifacts"
rm -f "$SELF_DIR"/llama-server "$SELF_DIR"/*.so "$SELF_DIR"/*.so.*

echo "==> building llama.cpp@$LLAMA_TAG + PR #22458 inside ubuntu:24.04"
echo "    (this clones llama.cpp + builds Vulkan; ~5-10 min depending on cores)"

# Run the build in a temp dir to avoid bind-mount root-ownership issues
BUILD_TMP=$(mktemp -d)
trap "rm -rf $BUILD_TMP" EXIT
cp "$PATCH" "$BUILD_TMP/22458.patch"

docker run --rm -v "$BUILD_TMP:/work" -w /work ubuntu:24.04 bash -c "
set -e
export DEBIAN_FRONTEND=noninteractive
echo '== install build deps =='
apt-get update -qq
apt-get install -y --no-install-recommends \\
    build-essential cmake git curl ca-certificates patchelf \\
    libvulkan-dev vulkan-tools glslc glslang-tools spirv-headers \\
    libcurl4-openssl-dev pkg-config 2>&1 | tail -3

echo '== clone llama.cpp@$LLAMA_TAG =='
git clone --branch $LLAMA_TAG --depth 1 https://github.com/ggml-org/llama.cpp.git src 2>&1 | tail -2
cd src

echo '== apply patch =='
git apply --check /work/22458.patch && git apply /work/22458.patch
grep -q 'may be NULL for device-only buffers' ggml/src/ggml-backend.cpp \\
    && echo '   patch verified in source' || { echo 'ERROR: patch did not apply'; exit 2; }

echo '== cmake configure =='
cmake -B build -DGGML_VULKAN=ON -DGGML_RPC=ON -DBUILD_SHARED_LIBS=ON \\
    -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \\
    -DLLAMA_CURL=ON 2>&1 | tail -5

echo '== build llama-server =='
cmake --build build -j\$(nproc) -t llama-server 2>&1 | tail -5

[ -f build/bin/llama-server ] || { echo 'ERROR: llama-server not built'; exit 3; }

echo '== set RUNPATH=\$ORIGIN on outputs =='
mkdir -p /work/out
cp -L build/bin/llama-server /work/out/
find build -name '*.so*' -exec cp -L {} /work/out/ \\;
cd /work/out
# CRITICAL: \$ORIGIN must be a literal RUNPATH value at runtime — not
# expanded. Inside this docker bash -c \"...\" string we need TWO levels of
# escape (here-string + container shell). The simpler form below uses
# 'single quotes' to suppress expansion in the container shell directly.
for f in llama-server *.so.*.*; do
    [ -L \"\$f\" ] && continue
    patchelf --set-rpath '\$ORIGIN' \"\$f\" || echo \"  patchelf failed on \$f\"
done
echo '== verify RUNPATH set =='
for f in llama-server libllama-common.so.0.0.1; do
    rp=\$(readelf -d \"\$f\" 2>/dev/null | grep -E 'RUNPATH|RPATH' | head -1)
    echo \"  \$f: \$rp\"
done

echo '== chown all of /work to caller uid =='
# CRITICAL: chown EVERYTHING in /work, not just /work/out — the cloned
# llama.cpp source tree is otherwise root-owned and the host-side trap
# 'rm -rf \$BUILD_TMP' fails with hundreds of 'Permission denied' lines.
chown -R $(id -u):$(id -g) /work 2>/dev/null || true
" 2>&1 | tail -25

# Move artifacts into our dir + dedupe with symlinks
cp -L "$BUILD_TMP/out/llama-server" "$SELF_DIR/"
chmod +x "$SELF_DIR/llama-server"
for f in "$BUILD_TMP/out/"*.so.*.*; do
    [ -f "$f" ] || continue
    [ -L "$f" ] && continue
    cp "$f" "$SELF_DIR/"
done

# Dedupe: replace duplicate .so / .so.0 with symlinks to the .so.X.Y.Z file.
# (Saves repo bloat if these ever land in version control. .gitignore covers this dir
# anyway, but symlinks also keep the dev tree tidy.)
cd "$SELF_DIR"
for base in libggml-base libggml-cpu libggml-rpc libggml libggml-vulkan libllama-common libllama libmtmd; do
    versioned=$(ls ${base}.so.*.* 2>/dev/null | head -1)
    if [ -n "$versioned" ]; then
        for short in ${base}.so ${base}.so.0; do
            if [ -f "$short" ] && [ ! -L "$short" ]; then
                rm -f "$short"
                ln -s "$versioned" "$short"
            fi
        done
    fi
done

echo
echo "==> build complete. Artifacts in $SELF_DIR:"
ls -la "$SELF_DIR/llama-server" "$SELF_DIR"/*.so* 2>/dev/null | head -10
echo
echo "==> deploy with: ./deploy.sh"
