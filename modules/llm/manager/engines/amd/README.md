<!-- SPDX-License-Identifier: BUSL-1.1 -->
# AMD data plane (D1) — DEFERRED (needs the gfx1151 bench box)

**Status: STUB / TODO.** Task **D1** in the Phase-1 breakdown is box work on an
AMD Strix Halo (gfx1151) node and is **not** implementable off-box. This dir is
a placeholder + spec so the engine wiring can be filled once the bench box is
scheduled (`0.91` time-share, per D18). It is **not referenced by any compose
file yet** — the Phase-1 manager (auth/enforce/proxy/metering/keys/metrics/
router-gen) is complete and tested independently.

## What D1 must deliver (DoD)

`llama-swap` fronting **our patched** `llama-server` (Vulkan) on gfx1151, serving
≥2 models on demand on the one 96 GB box, each reachable as an OpenAI `/v1`
endpoint that the manager registers in the LiteLLM router (R1).

## Hard-won AMD tuning that MUST carry over verbatim

(from the existing GPUStack `llm` profile — regressing any of these regresses
AMD stability; see the stack memory + CLAUDE.md):

- `llama-server` flags: `-fa 1 --no-mmap -ngl 999` (all layers to the 96 GB
  BIOS-pinned VRAM), and **`--cache-ram=0 --ctx-checkpoints=0`** (the default
  8 GiB host prompt-cache otherwise exhausts RAM — ga.4 fix).
- **gemma SWA**: use the patched `llama.cpp` build (b9112 + PR #22458); never
  `--swa-full` on gemma @1M ctx (≈256× SWA KV → host OOM).
- **qwen3 thinking-disable**: `extra_body.chat_template_kwargs.enable_thinking=false`.
- **MTP OFF on Vulkan**: MTP crashes the Vulkan runner (ROCm-only); keep the
  non-MTP build (b9305 note) until the ROCm-runner shift.
- No autoheal on any llama/gpustack-style container (cascade-kill storm).

## Intended shape (to implement in D1)

- `llama-swap.yaml` — model→command map; one `cmd:` per model with the flags
  above; `ttl` for idle unload; health endpoint per model.
- A compose overlay (`engines/amd/compose.yml`) adding a `llama-swap` service
  (Vulkan device passthrough: `/dev/kfd`, `/dev/dri`) bound to the manager's
  network, exposing `:8080/v1`. The scheduler's `/models/load` (Phase-2 node
  agent) drives llama-swap's API.

## G1 (end-to-end gate) — also deferred

Client (`rzfz-sk-…`) → Caddy → manager (auth+enforce+meter) → LiteLLM router →
`llama-server` on gfx1151; response streams; a `usage_events` row with
input/output/cached appears; `/metrics` reflects it; an over-limit key → 429.
Runs on 0.91 (time-share). **⏸ Stop-and-confirm before Phase 2** (operator-gated
by the spec). A runbook is in the night-run report.
