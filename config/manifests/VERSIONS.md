# Container Versions

Generated from `config/manifests/versions.json` — do not edit by hand. Run `scripts/generate-versions-md.py` after every version bump or module addition.

- **Stack version:** `2026.08-ga.2`
- **Channel:** `rc`
- **Published:** `2026-07-22`

## Core (always-on)

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| alpine | `alpine:3.24` | `core/compose.yml` | major | https://alpinelinux.org/releases/ | Base OS for the authentik-init + authentik-media-migrator one-shot init containers. 2026.08 (#194) bump 3.21 -> 3.24 (3.21 EOL ~Nov-2026; musl/openssl shift). Re-pulled at cut time. |
| authentik | `ghcr.io/goauthentik/server:2026.5.5` | `.env` `AUTHENTIK_VERSION` | minor | https://github.com/goauthentik/authentik/releases | Hard to upgrade; minor bumps only. Major version changes require stack upgrade. 2026.08 bump 2026.2.6 -> 2026.5.5 (feature-line 2026.2->2026.5 crossing): SSO chokepoint; runs DB migrations (watch the rc=1 ProxyProvider token-validity migration, guarded #147). Verify blueprints still load; live migration test required before fleet rollout. |
| autoheal | `willfarrell/autoheal:1.2.0` | `.env` `AUTOHEAL_VERSION` | patch | https://github.com/willfarrell/docker-autoheal/releases | M018 / S03.5 self-healing sidecar. Watches autoheal=true containers for unhealthy state and restarts them within ~120s. |
| docker-socket-proxy | `tecnativa/docker-socket-proxy:v0.4.2` | `compose.yml` | patch | https://github.com/Tecnativa/docker-socket-proxy/releases |  |
| postgres | `pgvector/pgvector:0.8.2-pg17` | `compose.yml` | frozen | https://github.com/pgvector/pgvector/releases | Major PostgreSQL version. Frozen — requires data migration for upgrades. |
| postgres-vanilla | `postgres:17.10` |  | major | https://hub.docker.com/_/postgres/tags | One-shot image used by authentik-migrate-reconcile (bridges 2025.10→2025.12 Authentik schema gap) and openwebui-migrate-reconcile (chat). 2026.07-rc1: pinned the previously-floating `postgres:17` tag to `postgres:17.10` in both core/compose.yml and modules/chat/compose.yml for reproducibility. |
| smtp-relay | `boky/postfix:v5.1.0` | `compose.yml` | patch | https://github.com/bokysan/docker-postfix/releases |  |
| valkey | `valkey/valkey:9.1.1` | `.env` `VALKEY_VERSION` | patch | https://github.com/valkey-io/valkey/releases | 9.1.1 SECURITY: CVE-2026-56684 (TLS UAF→RCE via CLIENT KILL) + CVE-2026-63639 (corrupt stream RDB→RCE) |

## Profile: chat

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| openwebui | `ghcr.io/open-webui/open-webui:0.10.2` | `.env` `OPENWEBUI_VERSION` | patch | https://github.com/open-webui/open-webui/releases | Frequent releases; patch bumps safe. Minor bumps may change API. 2026.07-rc1: 0.9.5 -> 0.10.2 — jumped past the known-bad 0.9.6 (no 0.9.7 was ever cut; the line moved straight to 0.10.x). |
| pipelines | `ghcr.io/open-webui/pipelines:git-039f9c5` | `.env` `PIPELINES_VERSION` | patch | https://github.com/open-webui/pipelines/releases | Upstream has no semver releases on GHCR — only ':main' and ':git-<sha>' tags. Pinned to a git-sha for immutability. Upstream main has not advanced since 2025-08-18. |

## Profile: dify

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| dify-api | `langgenius/dify-api:1.16.0` | `.env` `DIFY_VERSION` | patch | https://github.com/langgenius/dify/releases | Shared version for dify-api, dify-worker, dify-worker-beat. 2026.08 bump 1.15.0 -> 1.16.0: closes CVE-2026-41948 (DifyTap path-traversal, 9.4). Runs Dify DB migrations on first start; sync new upstream .env.dify keys from upstream docker/.env.example; upgrade-test on 0.91 before fleet. Tag has NO v-prefix (1.16.0; v1.16.0 is a phantom). |
| dify-plugin-daemon | `langgenius/dify-plugin-daemon:0.6.4-local` | `.env` `DIFY_PLUGIN_VERSION` | patch | https://github.com/langgenius/dify-plugin-daemon/releases | Uses -local suffix for non-cloud deployments. 2026.08 bump 0.6.3-local -> 0.6.4-local (bugfix; coupled to the dify-api 1.16.0 bump as one reviewed unit). |
| dify-sandbox | `langgenius/dify-sandbox:0.2.15` | `.env` `DIFY_SANDBOX_VERSION` | patch | https://github.com/langgenius/dify-sandbox/releases |  |

## Profile: llm-cpu

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| ollama-proxy | `eyalrot2/ollama-openai-proxy:0.7.0` | `llm/compose.yml` | patch | https://github.com/eyalrot2/ollama-openai-proxy/releases | Shared across llm-cpu, llm-box, llm-experimental. |

## Profile: llm-box

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gpustack-box-vulkan | `registry.gitlab.com/razzfazz.ai/razzfazz-ai-service-stack/gpustack:vulkan` | `llm/compose.yml` | frozen | — | GitLab Container Registry image built from llm/gpustack/Dockerfile.vulkan (see custom_built.razzfazz-gpustack for the source recipe). Frozen — AMD Vulkan compat; see milestone M018 for unfreeze. |

## Profile: llm-experimental

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gpustack-rocm-runner | `kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1` |  | minor | https://github.com/kyuz0/amd-strix-halo-toolboxes | Custom backend image registered in GPUStack 2.x as 'llama-box-custom' (server-side validation requires the -custom suffix). Spawned per model instance by gpustack via docker-socket-proxy. Run command: 'llama-server -m {{model_path}} --host 0.0.0.0 --port {{port}} -fa 1 --no-mmap -ngl 999'. -fa + --no-mmap mandatory on Strix Halo; -ngl 999 forces all layers to VRAM (96 GiB BIOS-pinned). |

## Profile: monitor

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| ferretdb | `ghcr.io/ferretdb/ferretdb:2.7.0` | `.env` `FERRETDB_VERSION` | patch | https://github.com/FerretDB/FerretDB/releases |  |
| komodo | `ghcr.io/moghtech/komodo-core:2.2.0` | `.env` `KOMODO_VERSION` | patch | https://github.com/moghtech/komodo/releases | Shared version for komodo-core and komodo-periphery. |
| komodo-db | `ghcr.io/ferretdb/postgres-documentdb:17-0.107.0-ferretdb-2.7.0` | `.env` `KOMODO_DB_VERSION` | patch | https://github.com/ferretdb/documentdb/releases | FerretDB PostgreSQL backend for Komodo. |
| komodo-periphery | `ghcr.io/moghtech/komodo-periphery:2.2.0` | `.env` `KOMODO_VERSION` | patch | https://github.com/moghtech/komodo/releases | Komodo node agent. Same version as komodo-core (uses KOMODO_VERSION env). Added 2026-05-13 — was missing from the manifest (M016 oversight). |

## Profile: searxng

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| searxng | `searxng/searxng:2026.7.19-6da6eee26` | `.env` `SEARXNG_VERSION` | minor | https://github.com/searxng/searxng/releases | Rolling release with date-based tags. Minor bumps safe. 2026.08 version-sweep (#195): 2026.7.3-747cec4c2 -> 2026.7.19-6da6eee26 (config-compatible rolling refresh). |

## Profile: stts

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| speaches | `ghcr.io/speaches-ai/speaches:0.8.3-cpu` | `.env` `SPEACHES_VERSION` | patch | https://github.com/speaches-ai/speaches/releases | CPU variant. -cpu suffix required. |

## Profile: gotenberg

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gotenberg | `gotenberg/gotenberg:8.34.0` | `.env` `GOTENBERG_VERSION` | patch | https://github.com/gotenberg/gotenberg/releases |  |

## Profile: gitea

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gitea | `gitea/gitea:1.27.0` | `.env` `GITEA_VERSION` | minor | https://github.com/go-gitea/gitea/releases | 2026.08 version-sweep (#195): 1.26.4 -> 1.27.0 — security-heavy minor (~43 fixed CVEs incl. CVE-2026-59765 SSRF->metadata, CVE-2026-58443, CVE-2026-58435 privesc, CVE-2026-58436 DoS). Runs automatic gitea_db migrations on first start — back up gitea_db and upgrade-test on 0.91 before fleet rollout. |

## Profile: lightrag

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| lightrag | `ghcr.io/hkuds/lightrag:v1.5.4` | `.env` `LIGHTRAG_VERSION` | patch | https://github.com/HKUDS/LightRAG/releases |  |

## Profile: cognee

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| cognee-mcp | `cognee/cognee-mcp:1.4.0` | `modules/knowledge/cognee/compose.yml` | semver-pinned (immutable); tracks the cognee backend line | https://hub.docker.com/r/cognee/cognee-mcp/tags | MCP sidecar exposing cognee memory (remember/recall/forget) as a streamable-HTTP MCP server (M035). Pinned to 1.2.2 to match the cognee backend base (#79); was commit-pinned main-5fedeec per security review GA5-F1 before upstream published semver mcp tags. 2026.08 bump 1.2.2 -> 1.4.0 in lockstep with the razzfazz-cognee base + frontend + authshim; pgvector/schema churn (wipe cognee-data) + ladybug 0.17.1 unchanged from 1.2.2 (vendored extension verified); the 0.16->0.17 on-disk migration is handled by #186 kuzu-migrate.py. |

## Profile: tika

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| tika | `apache/tika:3.3.1.0-full` | `doc-processing/tika/compose.yml` | patch | https://tika.apache.org/download.html |  |

## Profile: docling

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| docling | `ghcr.io/docling-project/docling-serve-cpu:v1.27.0` | `doc-processing/docling/compose.yml` | patch | https://github.com/docling-project/docling-serve/releases | CPU variant. |

## Profile: presidio

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| presidio-analyzer | `mcr.microsoft.com/presidio-analyzer:2.2.362` | `doc-processing/presidio/compose.yml` | patch | https://github.com/microsoft/presidio/releases |  |
| presidio-anonymizer | `mcr.microsoft.com/presidio-anonymizer:2.2.362` | `doc-processing/presidio/compose.yml` | patch | https://github.com/microsoft/presidio/releases |  |
| presidio-image-redactor | `mcr.microsoft.com/presidio-image-redactor:0.0.58` | `doc-processing/presidio/compose.yml` | patch | https://github.com/microsoft/presidio/releases |  |

## Profile: stirling-pdf

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| stirling-pdf | `stirlingtools/stirling-pdf:2.14.2` | `doc-processing/stirling-pdf/compose.yml` | patch | https://github.com/Stirling-Tools/Stirling-PDF/releases |  |

## Profile: matrix

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| element-web | `vectorim/element-web:v1.12.23` | `apps/matrix/compose.yml` | minor | https://github.com/element-hq/element-web/releases | Frontend only — minor bumps safe. |
| synapse | `matrixdotorg/synapse:v1.156.0` | `apps/matrix/compose.yml` | patch | https://github.com/element-hq/synapse/releases | Matrix homeserver. Patch bumps safe; minor requires migration check. |

## Profile: paperless-ngx

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| paperless-ngx | `ghcr.io/paperless-ngx/paperless-ngx:2.20.15` | `doc-processing/paperless-ngx/compose.yml` | patch | https://github.com/paperless-ngx/paperless-ngx/releases |  |

## Profile: vaultwarden

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| vaultwarden | `vaultwarden/server:1.36.0` | `apps/vaultwarden/compose.yml` | patch | https://github.com/dani-garcia/vaultwarden/releases |  |

## Profile: infisical

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| infisical | `infisical/infisical:v0.162.10` | `apps/infisical/compose.yml` | patch | https://github.com/Infisical/infisical/releases | Self-hosted secrets manager. As of v0.146.0 the upstream -postgres image variant was discontinued (last -postgres tag pushed 2025-08-08). We now use the default (non-suffixed) image and connect to our shared Postgres via DB_CONNECTION_URI. |

## Profile: onyx

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| onyx | `onyxdotapp/onyx-backend:v4.3.9` | `apps/onyx/compose.yml` | patch | https://github.com/onyx-dot-app/onyx/releases | Shared version for onyx-backend, onyx-web-server, onyx-model-server. 2026.08 bump v4.2.3 -> v4.3.9: 4.2->4.3 Postgres/Vespa index migration; move onyx-backend/web-server/model-server + vespa lockstep; upgrade-test the onyx profile. |
| onyx-model-server | `onyxdotapp/onyx-model-server:v4.3.9` | `apps/onyx/compose.yml` | patch | https://github.com/onyx-dot-app/onyx/releases | Shared version with onyx-backend (onyx release train). |
| onyx-web-server | `onyxdotapp/onyx-web-server:v4.3.9` | `apps/onyx/compose.yml` | patch | https://github.com/onyx-dot-app/onyx/releases | Shared version with onyx-backend (onyx release train). |
| vespa | `vespaengine/vespa:8.725.12` | `apps/onyx/compose.yml` | patch | https://github.com/vespa-engine/vespa/releases | 2026.08 bump 8.714.25 -> 8.725.12 (coupled to Onyx 4.3 Vespa schema; bump with onyx, not independently). |

## Profile: openhands

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| openhands | `ghcr.io/openhands/openhands:1.6.0` | `.env` `OPENHANDS_VERSION` | minor | https://github.com/All-Hands-AI/OpenHands/releases | OpenHands autonomous AI software development agent. Requires Docker socket. |
| openhands-runtime | `ghcr.io/openhands/runtime:1.6.0-nikolaik` | `apps/openhands/compose.yml` | patch | https://github.com/OpenHands/OpenHands/releases | Must match openhands version. |

## Profile: agents

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| hermes-agent | `ghcr.io/nousresearch/hermes-agent:v2026.7.1` |  | patch | https://github.com/NousResearch/hermes-agent/releases | Hermes agent base image (v2026.7.1 == hermes v0.18.0). Provisioned per-user by agent-manager. Built locally from public source (private GHCR). #36 Option B: SINGLE container — serves the built-in v0.18 dashboard on 9119 + gateway on 8642; the separate hermes-workspace companion was dropped. v0.18 uses s6-overlay + a mandatory dashboard auth gate (HERMES_DASHBOARD_BASIC_AUTH_*) and reads model.api_key from config.yaml. |
| moltis | `ghcr.io/moltis-org/moltis:20260719.01` | `moltis/compose.yml` | patch | https://github.com/moltis-org/moltis/releases | Rolling date-tagged release line; pin explicitly to a date tag. Provisioned per-user by agent-manager via agents/manager/app/services/catalog.py — no compose image: reference (M020). |
| openhands | `ghcr.io/openhands/openhands:1.6.0` | `apps/openhands/compose.yml` | patch | https://github.com/All-Hands-AI/OpenHands/releases | Provisioned per-user by agent-manager (M020); standalone openhands profile also available. Canonical image is ghcr.io/openhands/openhands (the ghcr.io/all-hands-ai/openhands mirror lags at 0.9.x). |

## Profile: crawl4ai

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| crawl4ai | `unclecode/crawl4ai:0.9.0` | `.env` `CRAWL4AI_VERSION` | minor | https://github.com/unclecode/crawl4ai/releases | RAG-friendly web crawler — Markdown extraction, structured scraping, browser automation. Apache-2.0. |

## Profile: llm

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gpustack | `gpustack/gpustack:v2.1.2` | `.env` `GPUSTACK_VERSION` | minor | https://github.com/gpustack/gpustack/releases | M018 unfreeze: 0.7.1 → v2.1.2 (upstream image, runs on ROCm 7.2 + 6.17 OEM kernel). M022 standard `llm` profile uses our custom llama-vulkan-runner (see llm/runners/llama-vulkan/Dockerfile) registered as the llama-box-vulkan-custom backend. The kyuz0 ROCm runner stays registered as llama-box-rocm-custom for A/B and as backup. Compose has 2.x-specific tweaks (--gateway-mode disabled, --api-port, extra_hosts host.docker.internal, --worker-ip via host-gateway) — see llm/compose.yml comments. |

## Profile: llm-legacy

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gpustack-legacy | `gpustack/gpustack:v0.7.1` | `.env` `GPUSTACK_LEGACY_TAG` | frozen | https://github.com/gpustack/gpustack/releases | Frozen 0.7.1 + custom Vulkan build (registry.gitlab.com/razzfazz.ai/.../gpustack:vulkan), kept as a one-flag rollback target for the M018 cycle. Will be removed once `llm` has been in production ≥30 days. |

## Profile: mac-llm

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| litellm-mac-gateway | `ghcr.io/berriai/litellm:main-v1.83.7-stable` | `.env` `LITELLM_VERSION` | minor | https://github.com/BerriAI/litellm/releases | LiteLLM proxy for the Mac LLM gateway (mac-llm profile, opt-in, 2026.08). LiteLLM releases fast; track the `-stable` line. SHIP GATE: repin to a reviewed tag / @sha256 digest at each release security review (new compose service trips pre-tag-check). |

## Profile: observability

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| clickhouse-server | `clickhouse/clickhouse-server:25.8.28.1` | `.env` `CLICKHOUSE_VERSION` | minor | https://github.com/ClickHouse/ClickHouse/releases | OpenLIT span/trace store (Apache-2.0). Memory-capped at 2g via compose + internal config tuning. 2026.07-rc1: migrated off the EOL 24.8 LTS (last upstream build 24.8.14.39 shipped 4 unpatched bundled-OpenSSL CVEs — CVE-2026-28388/-28389/-28390/-31790) to the supported 25.8 LTS line (pinned 25.8.25.37). observability is EXPERIMENTAL/opt-in and not in prod — needs a clean-install validation (enable observability profile) before GA. |
| openlit | `ghcr.io/openlit/openlit:1.24.1` | `.env` `OBSERVABILITY_VERSION` | minor | https://github.com/openlit/openlit/releases | OpenLIT — stack-wide LLM observability (Apache-2.0). Memory-capped at 512m via compose. |
| otel-collector | `otel/opentelemetry-collector-contrib:0.130.1` | `.env` `OTEL_COLLECTOR_VERSION` | minor | — | OTLP collector fronting OpenLIT + container-log export (#193/#197). |

## Locally-built images

Built from `Dockerfile`s in this repo. The `Upstream pin` column names what is fetched at build time (base image `FROM`, `git clone --branch`, pip/npm package pin). Entries marked “our code” ship only repo-local source.

| Container | Dockerfile | Upstream pin | Upstream releases | Notes |
|---|---|---|---|---|
| razzfazz-agent-manager | `agents/Dockerfile` | our code | — | Our code. |
| razzfazz-backup-management | `core/backup/manager/Dockerfile` | `python:3.11-alpine` | — | Our code. Web UI for backup ops. |
| razzfazz-backup-service | `core/backup/Dockerfile` | `offen/docker-volume-backup (via BACKUP_VERSION)` | https://github.com/offen/docker-volume-backup/releases | Wraps offen/docker-volume-backup. |
| razzfazz-caddy | `core/Caddy/Dockerfile` | `caddy:2.11.4` | https://github.com/caddyserver/caddy/releases | Reverse proxy, extended with plugins at build time. |
| razzfazz-coding-agent | `modules/agents/coding-agent-web/Dockerfile` | `ARG GSD_PI_VERSION=1.5.0 (@opengsd/gsd-pi), ARG OPENCODE_VERSION=1.18.4, ARG CODEX_VERSION=0.144.6 (overridden by .env)` | https://www.npmjs.com/package/@opengsd/gsd-pi | #36 coding-agent split — ONE parametrized Dockerfile builds four per-TYPE images (razzfazz-coding-agent-{opencode,gsd-pi,codex,user-defined}) via AGENT_KIND build arg. gsd MIGRATED from the deprecated `gsd-pi` npm pkg to the maintained `@opengsd/gsd-pi` (CLI binary unchanged: `gsd`). Shared tmux-backed multi-session web terminal. Codex is Apache-2.0 (binary bundled + LICENSE/NOTICE). Base node:24-bookworm-slim; web UI on :3004. |
| razzfazz-coding-agent-codex | `modules/agents/coding-agent-web/Dockerfile` | `AGENT_KIND=codex; ARG CODEX_VERSION=0.144.6 (overridden by .env CODEX_VERSION)` | https://github.com/openai/codex/releases | #36 coding-agent split — per-TYPE image built from the shared modules/agents/coding-agent-web/Dockerfile via AGENT_KIND=codex. Codex is Apache-2.0 (binary bundled + LICENSE/NOTICE). Provisioned by agent-manager. |
| razzfazz-coding-agent-opencode | `modules/agents/coding-agent-web/Dockerfile` | `AGENT_KIND=opencode; ARG OPENCODE_VERSION=1.18.4 (overridden by .env OPENCODE_VERSION)` | https://www.npmjs.com/package/opencode-ai | #36 coding-agent split — per-TYPE image built from the shared modules/agents/coding-agent-web/Dockerfile via AGENT_KIND=opencode. Sandboxed per-user tmux web terminal (:3004). Provisioned by agent-manager. |
| razzfazz-coding-agent-user-defined | `modules/agents/coding-agent-web/Dockerfile` | `AGENT_KIND=user-defined (no bundled coding CLI — user installs their own)` | — | #36 coding-agent split — per-TYPE image built from the shared modules/agents/coding-agent-web/Dockerfile via AGENT_KIND=user-defined. Bare sandbox for a user-supplied coding CLI. Provisioned by agent-manager. |
| razzfazz-coding-tools | `agents/coding-tools/Dockerfile` | `ARG GSD_PI_VERSION=3.0.0 (legacy deprecated gsd-pi pkg), ARG OPENCODE_VERSION=1.18.4 (overridden by .env)` | https://www.npmjs.com/package/gsd-pi | SUPERSEDED (#36) by the per-type coding-agent split (razzfazz-coding-agent-*). Kept buildable for existing provisioned instances. opencode aligned 1.17.5 -> 1.17.13. gsd stays on the OLD deprecated `gsd-pi` pkg (3.0.0, last release) here; the split builder uses the maintained @opengsd/gsd-pi. |
| razzfazz-cognee | `modules/knowledge/cognee/Dockerfile` | `FROM cognee/cognee:1.4.0` | https://github.com/topoteretes/cognee/releases | ladybug graph backend (embedded); adds litellm/tiktoken runtime patches via .pth and vendors the ladybug json extension so cognee needs no egress at build or startup (#79). 2026.08: base 1.2.2 -> 1.4.0. VERIFIED: 1.4.0 pins ladybug>=0.16.0,<0.18 (== 1.2.2); uv.lock resolves ladybug 0.17.1 -> extension-release 0.17.0, so LBUG_EXT_VER=0.17.0 is unchanged/correct (vendored binary sha256-matches upstream v0.17.0). #186 additionally vendors the 0.16.0 old-engine extension + kuzu-migrate.py so the whole 0.16-0.17 range is offline (no #79 deadlock at boot or in the migration export leg). Migration API present at cognee v1.4.0. TODO at cut: real build + live test + wipe/verify cognee-data (1.2->1.4 pgvector schema churn). |
| razzfazz-cognee-frontend | `modules/knowledge/cognee/frontend/Dockerfile` | `ARG COGNEE_TAG=v1.4.0` | https://github.com/topoteretes/cognee/releases | Next.js UI built from upstream cognee-frontend/ subtree (knowledge/cognee/frontend/Dockerfile). Pin MUST track razzfazz-cognee (same upstream tag) — bump both together. Upstream does not publish a frontend image; we git-clone the tag and build it. rc6.7 #57. 2026.08: COGNEE_TAG v1.2.2 -> v1.4.0 (tracks razzfazz-cognee). |
| razzfazz-config | `core/config/Dockerfile` | `python:3.11-alpine` | — | Our code. Configuration portal (M008). |
| razzfazz-dify-web | `modules/dify/Dockerfile` | `git clone langgenius/dify @ DIFY_VERSION (clone-at-build, #68)` | https://github.com/langgenius/dify/releases | Custom build of Dify web frontend; clone-at-build from the pinned DIFY_VERSION tag (no vendored source). base node:22-alpine. |
| razzfazz-gpustack | `llm/gpustack/Dockerfile.vulkan` | `git clone --branch v0.7.1` | https://github.com/gpustack/gpustack/releases | AMD GPU build; base rocm/rocm-terminal:6.4. |
| razzfazz-help | `core/help/Dockerfile` | `python:3.11-alpine` | — | Our code. |
| razzfazz-licenses | `core/licenses/Dockerfile` | `python:3.11-alpine` | — | Our code. |
| razzfazz-mcp-cognee-authshim | `modules/mcp-manager/cognee-authshim/Dockerfile` | `FROM cognee/cognee-mcp:1.4.0` | — | Our code. MCP auth shim fronting the per-user Cognee MCP proxy; built from modules/mcp-manager/cognee-authshim (mcp-manager #36). 2026.08: wraps cognee/cognee-mcp:1.4.0 (lockstep with the shared cognee-mcp sidecar). |
| razzfazz-mcp-manager | `modules/mcp-manager/manager/Dockerfile` | our code | — | Our code. Personal-MCP manager — provisions per-user MCP proxy instances from core/mcp/personal-mcp-catalog.yaml; served at mcp.<domain> (mcp-manager #36). |
| razzfazz-mcp-test-echo | `modules/mcp-manager/test-echo-mcp/Dockerfile` | our code | — | Our code. Minimal echo MCP server used to validate the mcp-manager provisioning path (catalog/test fixture). |
| razzfazz-model-sync | `llm/sync_models/Dockerfile.sync_models` | `python:3.11-slim` | — | Our code. |
| razzfazz-paperclip | `paperclip/Dockerfile` | `ARG PAPERCLIP_TAG=v2026.707.0 (overridden by .env PAPERCLIP_VERSION)` | — | Custom build; git-cloned upstream paperclipai/paperclip at PAPERCLIP_VERSION. |
| razzfazz-stack-hermes-agent | `modules/agents/hermes-agent/Dockerfile` | `ARG HERMES_AGENT_VERSION=v2026.7.1 (== hermes v0.18.0; overridden by .env HERMES_AGENT_VERSION)` | https://github.com/NousResearch/hermes-agent/releases | Locally-built per-user Hermes agent image (upstream ghcr.io/nousresearch/hermes-agent tracked as the runtime_only `hermes-agent` pin). #36 Option B single container: built-in v0.18 dashboard (:9119) + gateway (:8642). Provisioned per-user by agent-manager. |
| razzfazz-stack-moltis | `modules/agents/moltis/Dockerfile` | `ARG MOLTIS_VERSION=20260719.01 (overridden by .env MOLTIS_VERSION)` | https://github.com/moltis-org/moltis/releases | Locally-built per-user Moltis agent image (upstream ghcr.io/moltis-org/moltis tracked as the runtime_only `moltis` pin). Root-owned binary; managed update via MOLTIS_VERSION bump + re-provision (#139). Provisioned per-user by agent-manager. |
| razzfazz-start-portal | `core/start-portal/Dockerfile` | `python:3.11-alpine` | — | Our code. M028 start portal — post-login launcher served at start.<domain>; renders tiles based on core/start-portal/manifest.yaml + per-user prefs. |

_This file is regenerated from the manifest. To bump a version, edit `config/manifests/versions.json`, run `scripts/publish-manifest.sh --regenerate-checksum`, then `scripts/generate-versions-md.py`._
