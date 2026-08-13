#!/bin/sh
# Wrapper script: GPUStack v0.7.1 calls "llama-box" but we use llama-server (llama.cpp b8196).
# This wrapper strips llama-box-specific arguments that llama-server doesn't understand.
# Unlike llama-box, llama-server does NOT silently ignore unknown parameters.
#
# When invoked as "llama-box-rpc-server" (via symlink), GPUStack expects RPC server
# functionality. In that case, delegate to the real rpc-server binary instead of llama-server,
# since llama-server does not understand --rpc-server-host/port arguments.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INVOKED_AS="$(basename "$0")"

# If called as llama-box-rpc-server, use the real rpc-server binary.
# GPUStack v0.7.1 passes llama-box-specific args (--rpc-server-host, --rpc-server-port,
# --rpc-server-main-gpu, --origin-rpc-server-main-gpu) but the llama.cpp rpc-server
# binary expects --host and --port. We translate/filter accordingly.
if [ "$INVOKED_AS" = "llama-box-rpc-server" ]; then
    RPC_ARGS=""
    # NEXT_ACTION: "keep" = add next arg to RPC_ARGS, "skip" = drop next arg,
    # "rename_host" = add next arg as value after --host, "rename_port" = same for --port
    NEXT_ACTION="keep"
    for arg in "$@"; do
        case "$NEXT_ACTION" in
            skip)
                NEXT_ACTION="keep"
                continue
                ;;
            rename_host)
                RPC_ARGS="$RPC_ARGS --host $arg"
                NEXT_ACTION="keep"
                continue
                ;;
            rename_port)
                RPC_ARGS="$RPC_ARGS --port $arg"
                NEXT_ACTION="keep"
                continue
                ;;
        esac
        case "$arg" in
            --rpc-server-host)
                NEXT_ACTION="rename_host"
                ;;
            --rpc-server-host=*)
                RPC_ARGS="$RPC_ARGS --host ${arg#*=}"
                ;;
            --rpc-server-port)
                NEXT_ACTION="rename_port"
                ;;
            --rpc-server-port=*)
                RPC_ARGS="$RPC_ARGS --port ${arg#*=}"
                ;;
            --rpc-server-main-gpu|--origin-rpc-server-main-gpu)
                NEXT_ACTION="skip"
                ;;
            --rpc-server-main-gpu=*|--origin-rpc-server-main-gpu=*)
                ;;
            --rpc-server-cache)
                # llama-box flag; rpc-server uses --cache (no value)
                RPC_ARGS="$RPC_ARGS --cache"
                ;;
            --rpc-server-cache=*)
                RPC_ARGS="$RPC_ARGS --cache"
                ;;
            --rpc-server-cache-dir)
                # llama-box specific; rpc-server has no equivalent – skip flag + value
                NEXT_ACTION="skip"
                ;;
            --rpc-server-cache-dir=*)
                # skip =value form
                ;;
            --rpc-server-*)
                # Catch-all for any other unknown --rpc-server-* flags from llama-box.
                # Assume they take a value argument and skip both.
                NEXT_ACTION="skip"
                ;;
            *)
                RPC_ARGS="$RPC_ARGS $arg"
                ;;
        esac
    done
    exec "$SCRIPT_DIR/rpc-server" $RPC_ARGS
fi

LLAMA_SERVER="$SCRIPT_DIR/llama-server"

# Filter out unsupported arguments AND host-RAM-hostile defaults.
#
# Stripped:
#   --max-projected-cache N : llama-box specific, llama-server doesn't know it.
#   --no-mmap               : GPUStack v0.7.1 hardcodes this; on AMD Strix Halo it
#                             forces the model to be read into a ~27 GiB HOST RAM
#                             buffer before transfer to VRAM, and the buffer often
#                             isn't promptly released → OOM-killer fires on
#                             desktop processes when running two large models in
#                             parallel (2026-05-12 storm: gemma4 + qwen3.6 both
#                             27 GB Q8 → 54 GB host transient on a 31 GiB host
#                             slice). On unified-memory APUs mmap is free —
#                             kernel pages bytes from disk directly into the
#                             BIOS-pinned VRAM region without an intermediate
#                             host buffer. Default to mmap; if a specific load
#                             needs --no-mmap, set GPUSTACK_USE_NO_MMAP=1 on
#                             the gpustack container.
#   --no-warmup             : GPUStack v0.7.1 also hardcodes this. With mmap on
#                             (see above), --no-warmup defers ALL page faulting
#                             and Vulkan shader JIT to the first real user
#                             request → 30-90s cold-start tax per kill/respawn
#                             cycle. Warmup runs one dummy forward pass at boot
#                             that pre-faults pages, primes the page cache, and
#                             JIT-compiles Vulkan shaders. Cost: +10-30s on
#                             model spawn (paid once, before /health flips to
#                             200, so gpustack's readiness probe naturally
#                             waits). For dense models warmup fully eliminates
#                             cold-start. For MoE models warmup may not iterate
#                             every expert (depends on llama.cpp build) — if
#                             needed, supplement with a vmtouch prefault step.
#                             Set GPUSTACK_USE_NO_WARMUP=1 to restore the
#                             original (skip-warmup) behavior.
FILTERED_ARGS=""
SKIP_NEXT=0
for arg in "$@"; do
    if [ "$SKIP_NEXT" = "1" ]; then
        SKIP_NEXT=0
        continue
    fi
    case "$arg" in
        --max-projected-cache)
            SKIP_NEXT=1
            continue
            ;;
        --max-projected-cache=*)
            continue
            ;;
        --no-mmap)
            if [ "${GPUSTACK_USE_NO_MMAP:-0}" = "1" ]; then
                FILTERED_ARGS="$FILTERED_ARGS $arg"
            fi
            # else: silently drop — mmap default for Strix Halo APU memory model
            continue
            ;;
        --no-warmup)
            if [ "${GPUSTACK_USE_NO_WARMUP:-0}" = "1" ]; then
                FILTERED_ARGS="$FILTERED_ARGS $arg"
            fi
            # else: silently drop — keep warmup so first request is hot
            continue
            ;;
        *)
            FILTERED_ARGS="$FILTERED_ARGS $arg"
            ;;
    esac
done

exec "$LLAMA_SERVER" $FILTERED_ARGS
