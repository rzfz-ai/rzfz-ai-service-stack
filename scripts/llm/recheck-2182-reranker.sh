#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2182 re-check (read-only): the served reranker GGUF is the upstream artefact,
# the runner loaded it, and a rerank request answers.
#
# Read-only on the box: the models volume is mounted ':ro', the helper
# container is --rm with no network and --pull never, and nothing is stopped,
# removed or written. The bearer key is passed to curl through a 0600 config
# file (never on argv, where `ps` would show it).
set -euo pipefail
WANT=22c9979ce4fbcdc5acdc310c6641c32797eff1aa980b8f7a2db8a8ea23429a48
WANT_SIZE=639153184
F=qwen3-reranker-0.6b-q8_0.gguf
VOL=razzfazz-stack_llm-node-models
# The helper image the stack already ships (core/compose.yml runs alpine:3.24).
HELPER="${RZFZ_PACKAGE_HELPER_IMAGE:-alpine:3.24}"
KEY="${KEY:?export KEY}"
case "$KEY" in *$'\n'*|*$'\r'*) echo "KEY must be a single line" >&2; exit 2 ;; esac

umask 077
cfg="$(mktemp)"
trap 'rm -f "$cfg"' EXIT
esc="${KEY//\\/\\\\}"
esc="${esc//\"/\\\"}"
printf 'header = "Authorization: Bearer %s"\n' "$esc" > "$cfg"

got=$(docker run --rm --pull never --network none -v "${VOL}:/m:ro" "$HELPER" sha256sum "/m/$F" | cut -d' ' -f1) || got=""
size=$(docker run --rm --pull never --network none -v "${VOL}:/m:ro" "$HELPER" stat -c %s "/m/$F") || size=""
eng=$(docker ps --filter label=rzfz.role=llm-engine --format '{{.Names}} {{.Image}}' | grep -i rerank || true)
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 --config "$cfg" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-reranker","query":"what is a box","documents":["a box is a container","sky"]}' \
  "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/v1/rerank") || code="000"
echo "sha256 ${got:-<unreadable>} ($( [ "$got" = "$WANT" ] && echo match || echo MISMATCH )), ${size:-<unreadable>} bytes"
echo "engine: ${eng:-<none>}"
echo "rerank HTTP ${code}"
[ "$got" = "$WANT" ] && [ "$size" = "$WANT_SIZE" ] && [ "$code" = 200 ]
