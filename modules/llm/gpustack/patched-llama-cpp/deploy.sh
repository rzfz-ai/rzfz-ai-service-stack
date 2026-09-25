#!/bin/bash
# Deploy the patched llama.cpp build into a running gpustack container.
#
# Usage:
#   ./deploy.sh                                        # local docker, container 'gpustack'
#   GPUSTACK_CONTAINER=gpustack-legacy ./deploy.sh     # different container name
#   ssh worker 'bash -s' < deploy.sh                   # remote via stdin (artifacts must be there too)
#
# Idempotent: re-running just re-applies the same files. Safe to run any number of times.
# Restarts gpustack at the end so all 6 model llama-server children respawn with patched binary.

set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
GPUSTACK_CONTAINER="${GPUSTACK_CONTAINER:-gpustack}"
DEST_DIR=/usr/local/lib/python3.10/dist-packages/gpustack/third_party/bin/llama-box/llama-box-default

echo "==> deploying patched llama.cpp build to container '$GPUSTACK_CONTAINER'"

# Sanity check: container exists?
if ! docker inspect "$GPUSTACK_CONTAINER" >/dev/null 2>&1; then
    echo "ERROR: container '$GPUSTACK_CONTAINER' not found"
    exit 1
fi

# Sanity check: artifacts present in this dir? Build if not.
if [ ! -f "$SELF_DIR/llama-server" ]; then
    echo "==> binaries not present — running build.sh first"
    "$SELF_DIR/build.sh"
fi

# Backup current binaries (in case rollback needed)
echo "==> backing up current binaries to /tmp/llama-backup inside container"
docker exec "$GPUSTACK_CONTAINER" sh -c "
    mkdir -p /tmp/llama-backup
    cp -L $DEST_DIR/llama-server /tmp/llama-backup/ 2>/dev/null || true
    cp -L $DEST_DIR/*.so* /tmp/llama-backup/ 2>/dev/null || true
" || true

# Copy llama-server (a regular file)
echo "==> installing llama-server"
docker cp "$SELF_DIR/llama-server" "$GPUSTACK_CONTAINER:$DEST_DIR/llama-server"
docker exec "$GPUSTACK_CONTAINER" chmod +x "$DEST_DIR/llama-server"

# Copy versioned .so files (real content; the .so and .so.0 in our dir are symlinks
# to these and don't survive `docker cp` cleanly, so we re-create them after).
echo "==> installing versioned shared libs"
for f in "$SELF_DIR"/*.so.*.*; do
    [ -f "$f" ] || continue
    [ -L "$f" ] && continue   # skip symlinks
    docker cp "$f" "$GPUSTACK_CONTAINER:$DEST_DIR/"
done

# Re-create the .so and .so.0 symlinks on the destination so dlopen of
# libfoo.so.0 resolves to libfoo.so.0.X.Y.
echo "==> re-creating .so / .so.0 symlinks"
docker exec "$GPUSTACK_CONTAINER" sh -c "
    cd $DEST_DIR
    for base in libggml-base libggml-cpu libggml-rpc libggml libggml-vulkan libllama-common libllama libmtmd; do
        versioned=\$(ls \${base}.so.*.* 2>/dev/null | head -1)
        if [ -n \"\$versioned\" ]; then
            ln -sf \"\$versioned\" \${base}.so
            ln -sf \"\$versioned\" \${base}.so.0
        fi
    done
"

# Verify llama-server runs (catches glibc/runpath issues)
echo "==> sanity check: llama-server --version"
docker exec "$GPUSTACK_CONTAINER" "$DEST_DIR/llama-server" --version 2>&1 | head -3

echo "==> restarting gpustack to spawn all model children with patched binary"
docker restart "$GPUSTACK_CONTAINER"

echo
echo "==> deploy complete. Models will warm up over ~5 min."
echo "    Verify with: docker exec openwebui curl -s -H \"Authorization: Bearer \\\$GPUSTACK_API_KEY\" http://gpustack:9090/v1/models | python3 -c 'import sys,json; [print(m[\"name\"], m[\"ready_replicas\"]) for m in json.load(sys.stdin)[\"items\"]]'"
echo "    Rollback: docker exec $GPUSTACK_CONTAINER cp /tmp/llama-backup/* $DEST_DIR/ && docker restart $GPUSTACK_CONTAINER"
