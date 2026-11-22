<!-- SPDX-License-Identifier: BUSL-1.1 -->
# llm-worker-agent — #254 Phase-2 (SKELETON)

Per-node worker agent (spec §7). A lightweight FastAPI service on each fleet
node; the manager drives it remotely (no Kubernetes). **This is the endpoint
skeleton** — the real engine drivers (llama-swap/vLLM/Ollama), the atomic
multi-file model pull (Tier-1←0 pull-through), and the per-vendor GPU exporter
are Phase-2 box work, stubbed behind injectable providers so the §7 contract
is unit-tested off-box.

| Endpoint | Purpose | Skeleton behavior |
|---|---|---|
| `GET /health` | liveness + loaded instances | returns loaded instance ids |
| `GET /gpu` | GPU/VRAM | injectable probe; `unknown` off-box |
| `GET /metrics` | Prometheus | `node_agent_loaded_models` gauge |
| `POST /models/pull` | atomic multi-file pull | validates `files[]`; injectable puller |
| `POST /models/load` | start an instance | registers instance; **AMD: auto `--mmproj`** when an mmproj file is present |
| `POST /models/unload` | stop an instance (drain) | removes instance; 404 if unknown |

Engine-specific work still to do (Phase-2): AMD wraps the `llama-swap` API;
CUDA starts `vllm/vllm-openai` containers via the Docker socket; Mac drives
Ollama. GPU exporter per vendor (AMD parses `amd-smi`; NVIDIA DCGM; Mac host).

Service name **`llm-worker-agent`** (distinct from `modules/agents/manager`).
Not wired into the top-level compose — deployed per node.

Tests: `tests/unit/llm-node-agent/` (import guard + §7 contract).
