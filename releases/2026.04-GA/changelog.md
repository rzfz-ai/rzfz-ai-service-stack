# 🚀 razzfazz.ai — Release 2026.04-GA

**April 2026 General Availability — Zero-Touch Post-Install Provisioning & Experimental RAG Modules**

*April 2026 · 31 commits · Continued from 2026-03.GA.P2*

---

This release ships the long-awaited `razzfazz-post-install.sh` — a fully automated provisioning script that takes a freshly initialized stack from "services running" to "everything configured and ready to use" with a single command. No more manual GPUStack model deployments, no more clicking through Dify's setup wizard, no more hunting for the right API endpoints to connect Open WebUI to your inference backend.

Two experimental RAG modules join the stack: **LightRAG** for graph-aware retrieval-augmented generation backed by pgvector, and **Cognee** with FalkorDB for knowledge graph construction. Both are opt-in, fully SSO-integrated, and clearly marked as experimental.

In addition, this release introduces encrypted `.env` snapshots before every configuration change, a critical fix for AMD GPU inference reliability, and better configuration propagation between the `.env` file and the Open WebUI container.

---

## ✨ Highlights

### 🧠 LightRAG — Graph-Aware RAG [EXPERIMENTAL]

**Profile:** `lightrag` · **URL:** `https://rag.<domain>`

LightRAG brings graph-aware retrieval to the stack. Unlike traditional chunk-based RAG, LightRAG builds a knowledge graph from your documents and uses both vector similarity and graph traversal to answer queries — finding connections that flat vector search would miss.

This installation uses the standard `pgvector/pg17` PostgreSQL image (no AGE extension required). The LightRAG API is Authentik SSO-protected and supports the OpenAI-compatible endpoint format, making it directly usable from Open WebUI pipelines and Dify workflows.

Key configuration in `.env`:
- `LIGHTRAG_LLM_MODEL` — set by `razzfazz-post-install.sh` (e.g. `gemma4`)
- `LIGHTRAG_EMBEDDING_MODEL` — set by `razzfazz-post-install.sh` (e.g. `nomic-embed-text`)
- `LIGHTRAG_EMBEDDING_DIM` — `1024` for `nomic-embed-text`

### 🕸️ Cognee + FalkorDB — Knowledge Graph RAG [EXPERIMENTAL]

**Profile:** `cognee` · **URL:** `https://cognee.<domain>`

Cognee takes a different approach: it uses a multi-layer memory model to ingest, chunk, and connect information into a structured knowledge graph stored in FalkorDB (Redis-compatible graph database). It supports the full `cognify` → `search` pipeline and can ingest text, files, and URLs.

The Docker image is a custom extension of `cognee/cognee` with several compatibility patches applied at build time:

- **LiteLLM message ordering** — patches the message list to place `system` before `user` (required by strict OpenAI-compatible endpoints)
- **Instructor library integration** — disables `aiohttp` transport to avoid async loop conflicts with Cognee's own event loop
- **Embedding tokenizer mapping** — patches `MODEL_TO_TOKENIZER` to handle `openai/` prefixed model names from GPUStack

Both modules are gated behind their respective Compose profiles (`lightrag`, `cognee`) and are not included in any default init package preset. They share the existing PostgreSQL instance (dedicated databases `lightrag_db` and `cognee_db`) and integrate with GPUStack for LLM and embedding inference.

> ⚠️ **Experimental:** These modules are under active development. APIs, configuration, and behaviour may change between releases without a migration path.



### 🤖 razzfazz-post-install.sh — One Command to Full Operation

```bash
./razzfazz-post-install.sh --preset standard --verify
```

A 1,700-line orchestration script that fully provisions the stack after `razzfazz-init.sh` completes:

- **GPUStack:** Creates API key, deploys preset model portfolio, waits for all models to reach running state
- **Open WebUI:** Creates admin user, connects to GPUStack OpenAI endpoint, configures Speaches STT/TTS, sets up hybrid RAG search (embedding + reranking), configures SearXNG web search, updates model capabilities and hides utility models (embedding, reranker) from the chat UI
- **Dify:** Completes two-step initial setup, installs latest plugin versions from Marketplace, configures GPUStack as model provider, sets system model defaults (LLM, embedding, rerank)
- **Speaches:** Downloads STT model (`Systran/faster-whisper-small`) and TTS model (`ufozone/piper-de_DE-jarvis-high`)
- **Gitea:** Creates or updates admin user with correct domain email and password
- **Help Center:** Triggers documentation cache pre-warming for all 6 upstream doc sources
- **DNS:** Adds all subdomains to `/etc/hosts` for self-signed TLS installations
- **Verification Suite:** 17 API health checks spanning all major services with a pass/fail report

Two presets are available:

| Preset | LLM Models | Embedding | Reranker | Audio |
|--------|-----------|-----------|----------|-------|
| `standard` | gemma4, gemma4-audio, qwen3.5 | nomic-embed-text, qwen3-embedding | qwen3-reranker | ✓ |
| `developer` | gemma4, qwen3-coder-next, qwen3.5 | nomic-embed-text, qwen3-embedding | qwen3-reranker | ✓ |

The script is fully idempotent — safe to re-run on a stack that was partially provisioned, after a credentials rotation, or when adding new models.

### 🔒 Encrypted .env Snapshots Before Every Config Change

`scripts/env-snapshot.sh` creates an AES-256-CBC encrypted backup of `.env` and `.env.dify` before any configuration mutation — whether from `razzfazz-post-install.sh`, `razzfazz-setup.sh`, or `razzfazz-upgrade.sh`. Snapshots are stored in `.gsd/env-snapshots/` (excluded from git) and automatically rotated, keeping the last 50.

Encryption uses `BACKUP_ENCRYPTION_PASSWORD` from `.env`, falling back to `AUTHENTIK_BOOTSTRAP_PASSWORD`. To decrypt:

```bash
openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
  -in .gsd/env-snapshots/env-20260406-112207.tar.gz.enc \
  | tar xzf - -C /tmp/
```

### 🔧 GPUStack AMD GPU: Duplicate Server Header Fix

Fixed a hard crash in GPUStack's inference proxy on AMD GPU installations. When streaming responses from `llama.cpp`, the internal HTTP proxy would crash with:

```
Service unavailable. Original error: 400, message="Duplicate 'Server' header found."
```

Root cause: `llama.cpp` sends `Server: llama.cpp` in its response, and FastAPI/Uvicorn adds its own `Server: uvicorn` header when the worker proxies the response back to the master. `aiohttp 3.13` (used internally by GPUStack) is strict about duplicate headers and aborts the request.

Fix: the `Dockerfile.vulkan` now patches `gpustack/routes/worker/proxy.py` at image build time to strip the `Server` header from `llama.cpp` responses before they are forwarded.

---

## 🆕 What Changed

### Post-Install Provisioning (`razzfazz-post-install.sh`)
- Full GPUStack API key management with idempotent create/recreate logic
- Model deployment with configurable presets; waits for `running` state with progress output
- Open WebUI provisioning: admin signup or DB-based password reset, connection config via API
- Open WebUI model capabilities: hides `nomic-embed-text`, `qwen3-embedding`, `qwen3-reranker` from chat UI; disables `image_generation` on text models; ensures `vision: true` for multimodal models
- Open WebUI retrieval: sets `SEARXNG_QUERY_URL`, `ENABLE_RAG_HYBRID_SEARCH`, `RAG_RERANKING_MODEL`, etc. via `.env` (picked up on next container restart)
- Dify two-step setup: first validates `INIT_PASSWORD`, then creates admin account with plaintext password (setup API) vs. base64-encoded password (login API)
- Dify plugin installation: dynamically resolves latest plugin version from Marketplace, falls back to known-good version on API failure
- Dify model configuration: uses internal Dify Python ORM (via `docker exec`) to create GPUStack provider credentials with encrypted storage and configure workspace default models
- Gitea admin: idempotent user create/update, email synchronized to `razzfazz-ai-admin@<domain>`
- Help cache warming: triggered via `docker exec` directly into the `razzfazz-help` container (which has no host-mapped port), with correct Authentik group headers
- Verification suite: health checks for GPUStack (models, chat completion, embedding), Open WebUI (login, connection), Dify (setup status), SearXNG, Speaches, Authentik, and all supporting containers (Help, Licenses, Setup UI, Backup UI, Gitea)
- `--debug` flag enables `set -x` trace output for troubleshooting
- `--skip-models`, `--skip-dns`, `--skip-wait` flags for partial runs

### Encrypted .env Snapshots (`scripts/env-snapshot.sh`)
- AES-256-CBC encryption with PBKDF2 key derivation (100k iterations)
- Integrated into `razzfazz-post-install.sh` (before every `update_env_value` call) and `razzfazz-upgrade.sh` (before env migration)
- Snapshot cleanup uses `find` instead of glob patterns (avoids `set -eo pipefail` crash when no `.tar.gz` files exist)
- Stored in `.gsd/env-snapshots/` (added to `.gitignore`)

### Open WebUI (`chat/compose.yml`)
- Added `env_file: ../.env` to the `openwebui` service so post-install `.env` vars (`SEARXNG_QUERY_URL`, `ENABLE_RAG_HYBRID_SEARCH`, `RAG_RERANKING_MODEL`, `ENABLE_WEB_SEARCH`, etc.) are loaded by the container on startup — previously only `pipelines` had this

### GPUStack (`llm/gpustack/Dockerfile.vulkan`)
- Patch 8: monkey-patch `gpustack/routes/worker/proxy.py` at build time to strip the `Server` header from backend (`llama.cpp`) responses, preventing `aiohttp` 3.13 duplicate-header crash

### LightRAG Module (`lightrag/`) [EXPERIMENTAL]
- New service profile `lightrag` deploying the official `lightrag/lightrag` image
- PostgreSQL storage using existing `pgvector/pg17` instance (dedicated `lightrag_db` database, no AGE extension needed)
- Authentik SSO protection via Caddy forward-auth
- LightRAG API key and token secret auto-generated by `razzfazz-init.sh`
- Model and embedding configuration written by `razzfazz-post-install.sh` (`LIGHTRAG_LLM_MODEL`, `LIGHTRAG_EMBEDDING_MODEL`, `LIGHTRAG_EMBEDDING_DIM`)
- Reranking support via `LIGHTRAG_RERANK_BINDING`
- Full documentation page at `https://help.<domain>`
- Included in init wizard UI (profile selector, advanced config, review)

### Cognee Module (`cognee/`) [EXPERIMENTAL]
- New service profile `cognee` deploying a custom-built `cognee/cognee` image
- FalkorDB sidecar container for graph storage (Redis-compatible, profile-gated)
- Custom Dockerfile applies three compatibility patches at build time:
  - LiteLLM message ordering (system before user) for strict OpenAI endpoints
  - Disabled `aiohttp` transport in the Instructor library to avoid async conflicts
  - `MODEL_TO_TOKENIZER` mapping for `openai/`-prefixed GPUStack model names
- Authentik SSO protection via Caddy forward-auth
- Admin credentials aligned with stack-wide bootstrap password
- PostgreSQL `cognee_db` database created automatically by `init-db.sh`
- Model configuration written by `razzfazz-post-install.sh`
- Full documentation page at `https://help.<domain>`
- Included in init wizard UI and upgrade migration manifest

### Upgrade Script (`razzfazz-upgrade.sh`)
- Encrypted `.env` snapshot is taken before the env migration step

---

## 🐛 Bug Fixes

| Area | Fix |
|------|-----|
| **GPUStack AMD** | Streaming inference crashes with "Duplicate 'Server' header found" — fixed at image build time |
| **Post-Install** | `((var++))` arithmetic with `set -eo pipefail` causes silent exit — replaced with `var=$((var + 1))` |
| **Post-Install** | `ls *.tar.gz` glob in env-snapshot cleanup fails when no unencrypted snapshots exist — replaced with `find` |
| **Post-Install** | Dify setup sends base64-encoded password, but setup API expects plaintext — corrected encoding per endpoint |
| **Post-Install** | Dify model config script fails with `ModuleNotFoundError: No module named 'app'` — fixed with `sys.path.insert(0, '/app/api')` |
| **Post-Install** | Help cache pre-warming triggers before service restart, killing the background process — moved to Step 9 (post-restart) |
| **Post-Install** | Help cache API returns 403 because Authentik group headers are missing — added `X-Authentik-Groups` header |
| **Post-Install** | Help cache `curl` fails silently because `razzfazz-help` has no host-mapped port — switched to `docker exec` |
| **Post-Install** | Gitea provisioning uses `local` outside a function — encapsulated in `step_gitea_provisioning()` |
| **Verification** | GPUStack chat completion check fails for reasoning models (empty `content`, output in `reasoning_content`) |
| **Verification** | Login-based checks (GPUStack, Dify) were brittle and environment-dependent — replaced with endpoint health checks |

---

## 📊 By the Numbers

| Metric | Value |
|--------|-------|
| Commits since 2026.03-GA.P2 | 31 |
| Files changed | 6 |
| Lines added | 842 |
| Lines removed | 305 |
| New scripts | 2 (`razzfazz-post-install.sh`, `scripts/env-snapshot.sh`) |
| New experimental modules | 2 (LightRAG, Cognee + FalkorDB) |
| New env variables | 6 |
| Verification checks | 17 |
| Model presets | 2 (standard, developer) |

---

## ⬆️ Upgrade Path

```bash
# From 2026.03-GA.P2 (online)
./razzfazz-upgrade.sh

# Dry run first
./razzfazz-upgrade.sh --check
```

After the upgrade, run the post-install script to provision models and configure services:

```bash
./razzfazz-post-install.sh --preset standard --verify
```

> **Note for AMD GPU installations:** After `razzfazz-upgrade.sh`, rebuild the GPUStack image to get the duplicate-header patch:
> ```bash
> docker compose build gpustack-box
> docker compose up -d --force-recreate gpustack-box
> ```

---

## 🔧 New .env Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GITEA_ADMIN_EMAIL` | `razzfazz-ai-admin@${MAIN_DOMAIN}` | Gitea admin email (used by post-install) |
| `SMTP_RELAY_HOST` | `smtp-relay.gmail.com` | SMTP relay hostname |
| `LIGHTRAG_EMBEDDING_DIM` | `1024` | LightRAG embedding vector dimensions |
| `LIGHTRAG_EMBEDDING_TIMEOUT` | `120` | LightRAG embedding request timeout (seconds) |
| `LIGHTRAG_LLM_TIMEOUT` | `300` | LightRAG LLM request timeout (seconds) |
| `COGNEE_EMBEDDING_TOKENIZER` | `` | Cognee embedding tokenizer (optional override) |

---

Built with ❤️ by the razzfazz.ai team.

---

*Full diff: `git diff 2026.03-GA.P2..v2026.04-ga`*
