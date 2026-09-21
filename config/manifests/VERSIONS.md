# Container Versions

Generated from `config/manifests/versions.json` — do not edit by hand. Run `scripts/generate-versions-md.py` after every version bump or module addition.

- **Stack version:** `2026.09-ga`
- **Channel:** `ga`
- **Published:** `2026-08-06`

## Core (always-on)

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| alpine | `alpine:3.24` | `core/compose.yml` | major | https://alpinelinux.org/releases/ | Base OS for the authentik-init + authentik-media-migrator one-shot init containers. 2026.08 (#194) bump 3.21 -> 3.24 (3.21 EOL ~Nov-2026; musl/openssl shift). Re-pulled at cut time. |
| authentik | `ghcr.io/goauthentik/server:2026.5.7` | `.env` `AUTHENTIK_VERSION` | minor | https://github.com/goauthentik/authentik/releases | Hard to upgrade; minor bumps only. Major version changes require stack upgrade. 2026.08 bump 2026.2.6 -> 2026.5.5 (feature-line 2026.2->2026.5 crossing): SSO chokepoint; runs DB migrations (watch the rc=1 ProxyProvider token-validity migration, guarded #147). Verify blueprints still load; live migration test required before fleet rollout. 2026.09 Sicherheits-Bump vor dem Schnitt: 2026.5.5 -> 2026.5.7, dieselbe Patchlinie, KEIN Sprung der Funktionslinie. Der Treiber ist Django: 2026.5.5 traegt Django 5.2.15, 2026.5.7 traegt 5.2.17. Dazwischen behebt 5.2.16 DREI Sicherheitsfehler (CVE-2026-48588 privater Daten im geteilten Cache, CVE-2026-53877 Heap-Over-Read in GDAL-Rastern, CVE-2026-53878 Header-Injection ueber Zeilenumbrueche in Domainnamen — alle 'low') und 5.2.17 VIER (CVE-2026-15307 'high': serverseitiges Dateischreiben und Request-Forgery ueber raeumliche Lookups, ueber die Admin-Changelist erreichbar; plus zwei 'moderate' und eine 'low'). EINORDNUNG: CVE-2026-15307 braucht GeoDjango-Rasterfelder; dass Authentik solche registriert, ist NICHT geprueft und unwahrscheinlich — der Bump traegt sich aus der Summe und der Linientreue, nicht aus diesem einen Befund. Ausserdem in 2026.5.6: 'lifecycle/container: drop curl and runit' (weniger Angriffsflaeche im Bild) und in 2026.5.7 ein FIPS-Basisbild-Bump. Blueprint-Aenderungen in 2026.5.7 (ungueltiges YAML wird behandelt, dry run) betreffen uns unmittelbar, weil wir Blueprints ausliefern. Tag auf ghcr.io geprueft. Der Hinweis oben gilt unveraendert: Authentik migriert die Datenbank beim Start, der Live-Migrationstest bleibt Pflicht. |
| autoheal | `willfarrell/autoheal:1.2.0` | `compose.yml` | patch | https://github.com/willfarrell/docker-autoheal/releases | internal-tracking / S03.5 self-healing sidecar. The tag is HARD-CODED in core/compose.yml; the AUTOHEAL_VERSION recorded here previously existed neither in .env.example nor in any compose file, so the handbook's 'Pinned in' column named a knob no operator can find (found by the manifest guard, 2026-09-12). Watches autoheal=true containers for unhealthy state and restarts them within ~120s. |
| docker-socket-proxy | `tecnativa/docker-socket-proxy:v0.4.2` | `compose.yml` | patch | https://github.com/Tecnativa/docker-socket-proxy/releases |  |
| postgres | `pgvector/pgvector:0.8.2-pg17` | `compose.yml` | frozen | https://github.com/pgvector/pgvector/releases | Major PostgreSQL version. Frozen — requires data migration for upgrades. |
| postgres-vanilla | `postgres:17.10` |  | major | https://hub.docker.com/_/postgres/tags | One-shot image used by authentik-migrate-reconcile (bridges 2025.10→2025.12 Authentik schema gap) and openwebui-migrate-reconcile (chat). 2026.07-rc1: pinned the previously-floating `postgres:17` tag to `postgres:17.10` in both core/compose.yml and modules/chat/compose.yml for reproducibility. |
| smtp-relay | `boky/postfix:v5.1.0` | `compose.yml` | patch | https://github.com/bokysan/docker-postfix/releases |  |
| valkey | `valkey/valkey:9.1.2` | `.env` `VALKEY_VERSION` | patch | https://github.com/valkey-io/valkey/releases | 9.1.1 SECURITY: CVE-2026-56684 (TLS UAF→RCE via CLIENT KILL) + CVE-2026-63639 (corrupt stream RDB→RCE) 2026.09 Bump vor dem Schnitt: 9.1.1 -> 9.1.2, Patchstand. Tag auf Docker Hub geprueft. |

## Profile: chat

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| openwebui | `ghcr.io/open-webui/open-webui:0.11.3` | `.env` `OPENWEBUI_VERSION` | minor | https://github.com/open-webui/open-webui/releases | Frequent releases; patch bumps safe. Minor bumps may change API. 2026.07-rc1: 0.9.5 -> 0.10.2 — jumped past the known-bad 0.9.6 (no 0.9.7 was ever cut; the line moved straight to 0.10.x). 2026.09: 0.10.2 -> 0.11.0 (MINOR — 'The Interface, Reorganized' UI rewrite + Security Advisory; PostgreSQL chat-search data backfill on first boot scales with history; new ENABLE_OAUTH master toggle defaults True so SSO stays on). 2026.09 patch: 0.11.0 -> 0.11.1 (human-in-the-loop tool approval + models-that-ask-questions; no OAuth/OIDC change — the 0.10->0.11 login-500 #881 is fixed separately via the oauth PersistentConfig reconcile in cli/upgrade.sh). #249 #881. 2026.09 Sicherheits-Bump vor dem Schnitt: 0.11.1 -> 0.11.3. 0.11.2 nennt ausdruecklich Sicherheitskorrekturen und dass NICHT alle davon aufgezaehlt sind ('some may be withheld for a short time to give admins time to update') — ein Fall, in dem das Ausbleiben einer Liste selbst das Argument ist. Tag auf ghcr.io geprueft. |
| pipelines | `ghcr.io/open-webui/pipelines:git-039f9c5` | `.env` `PIPELINES_VERSION` | patch | https://github.com/open-webui/pipelines/releases | Upstream has no semver releases on GHCR — only ':main' and ':git-<sha>' tags. Pinned to a git-sha for immutability. Upstream main has not advanced since 2025-08-18. |

## Profile: dify

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| dify-api | `langgenius/dify-api:1.17.1` | `.env` `DIFY_VERSION` | patch | https://github.com/langgenius/dify/releases | Shared version for dify-api, dify-worker, dify-worker-beat. 2026.08 bump 1.15.0 -> 1.16.0: closes CVE-2026-41948 (DifyTap path-traversal, 9.4). Runs Dify DB migrations on first start; sync new upstream .env.dify keys from upstream docker/.env.example; upgrade-test on 0.91 before fleet. Tag has NO v-prefix (1.16.1; v1.16.1 is a phantom). 2026.09 bump 1.16.0 -> 1.16.1 (patch; 262-commit fix roll-up; additive migrations only; web recipe byte-identical; sandbox/plugin-daemon unchanged). #249. 2026.09 bump 1.16.1 -> 1.17.0 (minor): new Agent E2B sandbox + Skill management + workspace-level Skills; 7 new additive Alembic migrations (no manual backfill; run via flask db upgrade at container start); upstream renamed EDITION -> DEPLOYMENT_EDITION (not set anywhere in this repo's .env.dify.example, so no-op for us) and reworked the Agent-v2 backend env surface (DIFY_AGENT_SHELLCTL_* -> DIFY_AGENT_RUNTIME_BACKEND/DIFY_AGENT_LOCAL_SANDBOX_*; that whole section is INERT here — agent_backend/local_sandbox are not shipped, ENABLE_AGENT_V2=false). dify-plugin-daemon moves in lockstep (upstream 0.6.3-local -> 0.6.10-local on this release; we track upstream's pin, having previously run one patch ahead at 0.6.4-local). dify-sandbox (0.2.15) unchanged upstream. Web recipe: base image unchanged; ONE build-recipe line changed (VITE_GIT_HOOKS=0 pnpm install -> pnpm install --ignore-scripts), mirrored in modules/dify/Dockerfile. 2026.09 Sicherheits-Bump vor dem Schnitt: 1.17.0 -> 1.17.1. Upstream behebt 'Agent skills bypassed the SSRF private-network policy' — Agent-Verkehr lief ueber einen zweiten Proxy, der SSRF_PROXY_ALLOW_PRIVATE_IPS/_DOMAINS ignorierte, die fuer Workflows gesetzte Allowlist galt dort also nicht. Betrifft uns unmittelbar: DIFY_DOC_TOOLS_SSRF_ALLOW ist bei uns zugleich eine Authentifizierungsgrenze (#1548). Ausserdem: fehlerhafte Bildverweise liefen in die SSRF-Wiederholschleife und konnten einen Indexier-Arbeiter blockieren. Tag auf Docker Hub geprueft. |
| dify-plugin-daemon | `langgenius/dify-plugin-daemon:0.6.10-local` | `.env` `DIFY_PLUGIN_VERSION` | patch | https://github.com/langgenius/dify-plugin-daemon/releases | Uses -local suffix for non-cloud deployments. 2026.08 bump 0.6.3-local -> 0.6.4-local (bugfix; coupled to the dify-api 1.16.0 bump as one reviewed unit). 2026.09 bump 0.6.4-local -> 0.6.10-local, coupled to the dify-api 1.17.0 bump: upstream's own docker-compose.yaml moved from 0.6.3-local (at dify-api 1.16.1) to 0.6.10-local (at dify-api 1.17.0), overtaking our previous one-patch-ahead pin, so we now track upstream exactly again. |
| dify-sandbox | `langgenius/dify-sandbox:0.2.15` | `.env` `DIFY_SANDBOX_VERSION` | patch | https://github.com/langgenius/dify-sandbox/releases | 2026.09 (dify-api 1.16.1 -> 1.17.0 bump): checked upstream's docker-compose.yaml at the 1.17.0 tag — dify-sandbox pin is unchanged at 0.2.15, so this pin is NOT moved. |

## Profile: llm-legacy

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gpustack-cuda | `razzfazz-gpustack:cuda` | `modules/llm/compose.yml` | frozen | https://github.com/gpustack/gpustack/releases | gpustack 0.7.1 + custom CUDA llama.cpp (b9851, sm_120) — the NVIDIA/Blackwell stable path (#946). Same bare-local-name + pull_policy:never posture as gpustack-legacy (#270, Operator-Entscheid 2026-09-04 — the private SEQIS GitLab registry was retired and its host removed from the image name): NEVER fetched over the network, only produced by a local `docker build` (modules/llm/gpustack/Dockerfile.cuda; see custom_built.razzfazz-gpustack-cuda) or a `docker load` from an offline --include-images package (test_270). Existing boxes are retagged off the old name by cli/upgrade.sh::migrate_gpustack_image_rename (Step 5b.2). NVIDIA installs use this instead of the retired v2.1.x `llm` profile. #1448 (cutover C8, decision D4) merged the former `llm-cuda` profile into `llm-legacy`, where HARDWARE=nvidia selects this image through modules/llm/compose.devices.nvidia.yml; cli/upgrade.sh rewrites an existing box's token. |
| gpustack-legacy | `razzfazz-gpustack:vulkan` | `modules/llm/compose.yml` | frozen | https://github.com/gpustack/gpustack/releases | Frozen gpustack 0.7.1 + custom Vulkan build — the stable AMD-Vulkan backend module (llm-legacy). #270 (Operator-Entscheid 2026-09-04): the image is a BARE LOCAL NAME with no registry host. It carried the private SEQIS GitLab Container Registry path until that registry was retired („komplett outdated ... seit Monaten nicht im Einsatz, wir liefern immer alle images mit, auch schon immer“). It is `pull_policy: never` in both modules/llm/compose.yml and compose.no-build.yml (see tests/unit/consistency/test_270_no_private_registry.py), so it is NEVER fetched over the network by any box — only a local `docker build` (modules/llm/gpustack/Dockerfile.vulkan; see custom_built.razzfazz-gpustack) or a `docker load` from an offline --include-images package. Existing boxes are retagged off the old name by cli/upgrade.sh::migrate_gpustack_image_rename (Step 5b.2); the old tag is deliberately kept so `rzfz upgrade --rollback` still finds it. Historical SBOMs under releases/ keep the old name on purpose — they record what actually shipped. |
| ollama-proxy | `eyalrot2/ollama-openai-proxy:0.7.0` | `llm/compose.yml` | patch | https://github.com/eyalrot2/ollama-openai-proxy/releases | The `llm-cpu` profile that used to share this was merged into `llm-legacy` (#1447 part b). |

## Profile: monitor

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| ferretdb | `ghcr.io/ferretdb/ferretdb:2.7.0` | `.env` `FERRETDB_VERSION` | patch | https://github.com/FerretDB/FerretDB/releases |  |
| komodo | `ghcr.io/moghtech/komodo-core:2.3.3` | `.env` `KOMODO_VERSION` | patch | https://github.com/moghtech/komodo/releases | Shared version for komodo-core and komodo-periphery. 2026.09 Bump vor dem Schnitt: 2.2.0 -> 2.3.3. Teilt sich KOMODO_VERSION mit komodo-periphery — beide Bilder bewegen sich zusammen, sonst reden Kern und Peripherie ueber verschiedene Protokollstaende. Tag auf ghcr.io geprueft. |
| komodo-db | `ghcr.io/ferretdb/postgres-documentdb:17-0.107.0-ferretdb-2.7.0` | `.env` `KOMODO_DB_VERSION` | patch | https://github.com/ferretdb/documentdb/releases | FerretDB PostgreSQL backend for Komodo. |
| komodo-periphery | `ghcr.io/moghtech/komodo-periphery:2.3.3` | `.env` `KOMODO_VERSION` | patch | https://github.com/moghtech/komodo/releases | Komodo node agent. Same version as komodo-core (uses KOMODO_VERSION env). Added 2026-05-13 — was missing from the manifest (internal-tracking oversight). |

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
| gotenberg | `gotenberg/gotenberg:8.37.0` | `.env` `GOTENBERG_VERSION` | patch | https://github.com/gotenberg/gotenberg/releases | 2026.09 Sicherheits-Bump vor dem Schnitt: 8.34.0 -> 8.37.0. Alle drei Zwischenversionen tragen einen eigenen Abschnitt 'Security Fixes'; 8.36.0 nennt darunter, dass Chromium WebSocket-Verbindungen zu Hosts oeffnen konnte, die die SSRF-Politik sonst sperrt. Tag auf Docker Hub geprueft. |

## Profile: gitea

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| gitea | `gitea/gitea:1.27.3` | `.env` `GITEA_VERSION` | minor | https://github.com/go-gitea/gitea/releases | 2026.08 version-sweep (#195): 1.26.4 -> 1.27.0 — security-heavy minor (~43 fixed CVEs incl. CVE-2026-59765 SSRF->metadata, CVE-2026-58443, CVE-2026-58435 privesc, CVE-2026-58436 DoS). Runs automatic gitea_db migrations on first start — back up gitea_db and upgrade-test on 0.91 before fleet rollout. 2026.09 Sicherheits-Bump vor dem Schnitt: 1.27.1 -> 1.27.3 auf derselben Nebenlinie. Die 1.27er-Linie ist sicherheitslastig (vgl. den 1.26->1.27-Eintrag oben). Kein Datenbankschritt ueber Patchversionen erwartet; der Upgrade-Test auf einer wegwerfbaren Box bleibt trotzdem Pflicht, weil gitea beim Start migriert. Tag auf Docker Hub geprueft. |

## Profile: lightrag

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| lightrag | `ghcr.io/hkuds/lightrag:v1.5.7` | `.env` `LIGHTRAG_VERSION` | patch | https://github.com/HKUDS/LightRAG/releases | 2026.09 Sicherheits-Bump vor dem Schnitt: v1.5.5 -> v1.5.7. Zwei Sicherheitshinweise: GHSA-25c3-j78v-83qx (Markdown-Bilddownloads je Dokument begrenzt) und GHSA-c922-pw4m-4wcv (Attributpruefung an den manuellen Entity-/Relation-APIs). Tag auf ghcr.io geprueft. |

## Profile: cognee

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| cognee-mcp | `cognee/cognee-mcp:1.5.3.dev1` | `modules/knowledge/cognee/compose.yml` | patch | https://hub.docker.com/r/cognee/cognee-mcp/tags | semver-pinned; tracks the cognee backend line in lockstep (no longer declared immutable — unfrozen 2026.08 per operator directive). MCP sidecar exposing cognee memory (remember/recall/forget) as a streamable-HTTP MCP server (internal-tracking). Was pinned to 1.2.2 to match the cognee backend base (#79); was commit-pinned main-5fedeec per security review GA5-F1 before upstream published semver mcp tags. 2026.08 bump #1: 1.2.2 -> 1.4.0 in lockstep with the razzfazz-cognee base + frontend + authshim; pgvector/schema churn (wiped cognee-data) + ladybug 0.17.1 unchanged from 1.2.2. 2026.08 bump #2 (unfreeze): 1.4.0 -> 1.5.3, same lockstep set. Verified directly against upstream pyproject.toml + GitHub release notes for every tag v1.4.1..v1.5.3: no breaking API/schema changes reported anywhere in that range, and the pgvector client pin (pgvector>=0.3.5,<0.4) is unchanged — so, unlike the 1.2->1.4 jump, this bump needs NO cognee-data wipe and NO re-cognify. It DOES cross a ladybug graph-backend line: v1.5.0 bumped the ladybug pin from >=0.16.0,<=0.18.2 to ==0.19.0 (Linux), an on-disk storage-format change (cognee_db_workers.ladybug_migrate.ladybug_version_mapping: 0.17.1 -> code 41, 0.19.0 -> code 43). The existing #186 kuzu-migrate.py handles this generically (storage-format major.minor mismatch -> cognee's own ladybug_migration() export/import API, single hop regardless of version gap) with NO code change required; the Dockerfile now also vendors the new v0.19.0 JSON extension (sha256-distinct from 0.17.0 despite identical size) alongside the retained 0.17.0/0.16.0 tiers so the migration's old-engine EXPORT leg stays fully offline (#79) for boxes at either prior on-disk format. |

## Profile: tika

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| tika | `apache/tika:3.3.1.0-full` | `doc-processing/tika/compose.yml` | patch | https://tika.apache.org/download.html |  |

## Profile: docling

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| docling | `ghcr.io/docling-project/docling-serve-cpu:v1.32.0` | `doc-processing/docling/compose.yml` | patch | https://github.com/docling-project/docling-serve/releases | CPU variant. 2026.09 Bump vor dem Schnitt: v1.31.0 -> v1.32.0. Tag in der ghcr-Tagliste von docling-serve-cpu geprueft (NICHT docling-serve — wir fahren die CPU-Variante, und das sind getrennte Repositorien). |

## Profile: presidio

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| presidio-analyzer | `mcr.microsoft.com/presidio-analyzer:2.2.362` | `doc-processing/presidio/compose.yml` | patch | https://github.com/microsoft/presidio/releases |  |
| presidio-anonymizer | `mcr.microsoft.com/presidio-anonymizer:2.2.362` | `doc-processing/presidio/compose.yml` | patch | https://github.com/microsoft/presidio/releases |  |
| presidio-image-redactor | `mcr.microsoft.com/presidio-image-redactor:0.0.58` | `doc-processing/presidio/compose.yml` | patch | https://github.com/microsoft/presidio/releases |  |

## Profile: stirling-pdf

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| stirling-pdf | `stirlingtools/stirling-pdf:2.14.3` | `doc-processing/stirling-pdf/compose.yml` | patch | https://github.com/Stirling-Tools/Stirling-PDF/releases |  |

## Profile: matrix

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| element-web | `vectorim/element-web:v1.12.27` | `apps/matrix/compose.yml` | minor | https://github.com/element-hq/element-web/releases | Frontend only — minor bumps safe. 2026.09 Sicherheits-Bump vor dem Schnitt: v1.12.25 -> v1.12.27. v1.12.27 nennt GHSA-9r5h-8m2x-w7q6. Tag auf Docker Hub geprueft. |
| synapse | `matrixdotorg/synapse:v1.158.0` | `apps/matrix/compose.yml` | patch | https://github.com/element-hq/synapse/releases | Matrix homeserver. Patch bumps safe; minor requires migration check. |

## Profile: paperless-ngx

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| paperless-ngx | `ghcr.io/paperless-ngx/paperless-ngx:2.20.15` | `doc-processing/paperless-ngx/compose.yml` | patch | https://github.com/paperless-ngx/paperless-ngx/releases |  |

## Profile: vaultwarden

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| vaultwarden | `vaultwarden/server:1.37.2` | `apps/vaultwarden/compose.yml` | patch | https://github.com/dani-garcia/vaultwarden/releases |  |

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
| hermes-agent | `ghcr.io/nousresearch/hermes-agent:v2026.8.27` |  | patch | https://github.com/NousResearch/hermes-agent/releases | Hermes agent base image (v2026.7.1 == hermes v0.18.0). Provisioned per-user by agent-manager. Built locally from public source (private GHCR). #36 Option B: SINGLE container — serves the built-in v0.18 dashboard on 9119 + gateway on 8642; the separate hermes-workspace companion was dropped. v0.18 uses s6-overlay + a mandatory dashboard auth gate (HERMES_DASHBOARD_BASIC_AUTH_*) and reads model.api_key from config.yaml. |
| moltis | `ghcr.io/moltis-org/moltis:20260827.01` | `moltis/compose.yml` | patch | https://github.com/moltis-org/moltis/releases | Rolling date-tagged release line; pin explicitly to a date tag. Provisioned per-user by agent-manager via agents/manager/app/services/catalog.py — no compose image: reference (internal-tracking). |
| openhands | `ghcr.io/openhands/openhands:1.6.0` | `apps/openhands/compose.yml` | patch | https://github.com/All-Hands-AI/OpenHands/releases | Provisioned per-user by agent-manager (internal-tracking); standalone openhands profile also available. Canonical image is ghcr.io/openhands/openhands (the ghcr.io/all-hands-ai/openhands mirror lags at 0.9.x). |

## Profile: crawl4ai

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| crawl4ai | `unclecode/crawl4ai:0.9.3` | `.env` `CRAWL4AI_VERSION` | minor | https://github.com/unclecode/crawl4ai/releases | RAG-friendly web crawler — Markdown extraction, structured scraping, browser automation. Apache-2.0. 2026.09 Bump vor dem Schnitt: 0.9.0 -> 0.9.3, Patchstand. Tag auf Docker Hub geprueft. |

## Profile: llm-registry

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| llm-registry-zot | `ghcr.io/project-zot/zot-linux-amd64:v2.1.21` | `.env` `ZOT_VERSION` | minor | https://github.com/project-zot/zot/releases | Zot OCI registry for LLM Manager model-artifact distribution (llm-registry profile, part of the always-on manager trio since #1443). Apache-2.0, single static binary. SHIP GATE: repin to a reviewed tag / @sha256 digest at each release security review (new compose service trips pre-tag-check). 2026.09 Sicherheits-Bump vor dem Schnitt: v2.1.3 -> v2.1.21 (18 Patchversionen). Darunter v2.1.14 mit CVE-2025-30204 und v2.1.18 mit einer Maskierung sensibler Schluessel in der Konfigurationsausgabe. Tag in der ghcr-Tagliste geprueft (eine Manifest-Abfrage antwortet auf diesem distroless-Bild nicht verlaesslich — die Tagliste ist die Quelle). |

## Profile: llm-worker-agent

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| llama-rocm-runner-base | `kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1` |  | minor | — | Base image of modules/llm/runners/llama-rocm/Dockerfile — the ROCm llama.cpp runner the LLM Manager's worker agent launches on AMD (#1516 tag scheme: llama-runner:<engine-build>-rocm). RENAMED from 'gpustack-rocm-runner' by #1447: the pin is unchanged and still live, but it no longer names a GPUStack 2.x custom backend — 2.x and its 'llm' profile were removed. -fa + --no-mmap are mandatory on Strix Halo; -ngl 999 forces all layers to VRAM (96 GiB BIOS-pinned). |

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
| postgres-exporter | `quay.io/prometheuscommunity/postgres-exporter:v0.20.1` | `.env` `POSTGRES_EXPORTER_VERSION` | minor | https://github.com/prometheus-community/postgres_exporter/releases | #253 X5 S3: postgres /metrics for the customer scrape surface. Docker-network only (expose 9187); consumed solely by the otel-collector scrape overlay. |
| valkey-exporter | `oliver006/redis_exporter:v1.91.1-alpine` | `.env` `REDIS_EXPORTER_VERSION` | minor | https://github.com/oliver006/redis_exporter/releases | #253 X5 S3: valkey /metrics (redis_exporter speaks RESP, works against Valkey unchanged). Docker-network only (expose 9121). 2026.09 Bump vor dem Schnitt: v1.89.0-alpine -> v1.91.1-alpine. Die Alpine-Variante ist beibehalten — der blosse Upstream-Tag v1.91.1 waere eine ANDERE Bildvariante. Tag auf Docker Hub geprueft. |

## Profile: openuem

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| openuem-cert-manager | `openuem/openuem-cert-manager:0.12.0` | `.env` `OPENUEM_VERSION` | minor | https://github.com/open-uem/openuem-cert-manager/releases | OpenUEM one-shot PKI bootstrap (own CA, component certs, agent enrolment keypair, admin .pfx, nats.cfg). Version-locked to the console. Apache-2.0. |
| openuem-console | `openuem/openuem-console:0.12.0` | `.env` `OPENUEM_VERSION` | minor | https://github.com/open-uem/openuem-console/releases | OpenUEM web console (Echo + HTMX). Version-locked to the workers, cert-manager and OCSP responder — bump all four together. Apache-2.0. |
| openuem-nats | `nats:2.14.6-alpine` | `apps/openuem/compose.yml` | minor | https://github.com/nats-io/nats-server/releases | NATS + JetStream broker for the OpenUEM agents. Alpine variant on purpose: the plain image has no shell for the healthcheck. Its config is GENERATED by the cert bootstrap, not vendored. Apache-2.0. |
| openuem-ocsp-responder | `openuem/openuem-ocsp-responder:0.12.0` | `.env` `OPENUEM_VERSION` | minor | https://github.com/open-uem/openuem-ocsp-responder/releases | OpenUEM OCSP responder — revocation checks for the module's own CA; consulted by the NATS broker on every agent connect. Version-locked to the console. Apache-2.0. |
| openuem-worker | `openuem/openuem-worker:0.12.0` | `.env` `OPENUEM_VERSION` | minor | https://github.com/open-uem/openuem-worker/releases | OpenUEM agent/cert-manager/notification workers (one image, three commands). Version-locked to the console. Apache-2.0. |

## Profile: wazuh

| Component | Image : tag | Pinned in | Compatibility | Releases | Notes |
|---|---|---|---|---|---|
| wazuh-certs-generator | `wazuh/wazuh-certs-generator:0.0.4` | `apps/wazuh/certs-generator/Dockerfile` | patch | https://github.com/wazuh/wazuh-docker/releases | One-shot PKI generator. Pinned literally, NOT to WAZUH_VERSION — Wazuh versions it independently. 0.0.2 was the tag their own 4.14.7 reference compose used; the 2026.09 pre-cut bump moved us to 0.0.4 deliberately (we had been sitting on a 2024-03 base). Our own image tag `razzfazz-wazuh-certs-generator:<v>` follows this version, in modules/apps/wazuh/compose.yml, compose.no-build.yml and core/config/profiles.yaml — all three were missed by the first bump and the guards that caught it are named in test_855_wazuh_certs.py. #1277: the pin now lives in apps/wazuh/certs-generator/Dockerfile (ARG WAZUH_CERTS_GENERATOR_VERSION), because the compose service builds our own thin image on top. Upstream downloaded wazuh-certs-tool.sh from packages.wazuh.com at FIRST RUN (DECISION-7), which no offline box (#184) can do; the tool is now fetched and sha256-verified at BUILD time. 2026.09 Bump vor dem Schnitt: 0.0.2 -> 0.0.4. Wir standen auf einem Stand von 2024-03; 0.0.3 (2025-09) und 0.0.4 (2025-12) liegen dazwischen. Der ARG-Wert im Dockerfile und dieser Eintrag bewegen sich zusammen. Tag auf Docker Hub geprueft. ACHTUNG fuer den Boxnachweis: dieses Bild erzeugt die Zertifikatskette des ganzen Wazuh-Stapels — ein Wechsel gehoert auf einer wegwerfbaren Box geprueft, bevor er eine Flotte sieht. |
| wazuh-dashboard | `wazuh/wazuh-dashboard:4.14.7` | `.env` `WAZUH_VERSION` | minor | https://github.com/wazuh/wazuh/releases | OpenSearch-Dashboards fork — the only Caddy-routed Wazuh surface (forward_auth + native OIDC). Apache-2.0. |
| wazuh-indexer | `wazuh/wazuh-indexer:4.14.7` | `.env` `WAZUH_VERSION` | minor | https://github.com/wazuh/wazuh/releases | OpenSearch-fork event index (version-locked to wazuh-manager). Needs vm.max_map_count >= 262144 (core/sysctl/99-razzfazz-stability.conf). Apache-2.0. |
| wazuh-manager | `wazuh/wazuh-manager:4.14.7` | `.env` `WAZUH_VERSION` | minor | https://github.com/wazuh/wazuh/releases | SIEM/XDR manager — rule engine, agent enrollment/event listener. Version-locked with wazuh-indexer + wazuh-dashboard; bump all three together or the cluster refuses to form. Container paths moved between 4.9 and 4.14 (indexer config under config/) — re-read the upstream reference compose on any minor bump. GPL-2.0. |

## Locally-built images

Built from `Dockerfile`s in this repo. The `Upstream pin` column names what is fetched at build time (base image `FROM`, `git clone --branch`, pip/npm package pin). Entries marked “our code” ship only repo-local source.

| Container | Dockerfile | Upstream pin | Upstream releases | Notes |
|---|---|---|---|---|
| razzfazz-agent-manager | `agents/Dockerfile` | our code | — | Our code. |
| razzfazz-backup-management | `core/backup/manager/Dockerfile` | `python:3.11-alpine` | — | Our code. Web UI for backup ops. |
| razzfazz-backup-service | `core/backup/Dockerfile` | `offen/docker-volume-backup (via BACKUP_VERSION)` | https://github.com/offen/docker-volume-backup/releases | Wraps offen/docker-volume-backup. |
| razzfazz-caddy | `core/Caddy/Dockerfile` | `caddy:2.11.4` | https://github.com/caddyserver/caddy/releases | Reverse proxy, extended with plugins at build time. |
| razzfazz-coding-agent | `modules/agents/coding-agent-web/Dockerfile` | `ARG GSD_PI_VERSION=1.5.0 (@opengsd/gsd-pi), ARG OPENCODE_VERSION=1.18.25, ARG CODEX_VERSION=0.151.0 (overridden by .env)` | https://www.npmjs.com/package/@opengsd/gsd-pi | #36 coding-agent split — ONE parametrized Dockerfile builds four per-TYPE images (razzfazz-coding-agent-{opencode,gsd-pi,codex,user-defined}) via AGENT_KIND build arg. gsd MIGRATED from the deprecated `gsd-pi` npm pkg to the maintained `@opengsd/gsd-pi` (CLI binary unchanged: `gsd`). Shared tmux-backed multi-session web terminal. Codex is Apache-2.0 (binary bundled + LICENSE/NOTICE). Base node:24-bookworm-slim; web UI on :3004. |
| razzfazz-coding-agent-codex | `modules/agents/coding-agent-web/Dockerfile` | `AGENT_KIND=codex; ARG CODEX_VERSION=0.151.0 (overridden by .env CODEX_VERSION)` | https://github.com/openai/codex/releases | #36 coding-agent split — per-TYPE image built from the shared modules/agents/coding-agent-web/Dockerfile via AGENT_KIND=codex. Codex is Apache-2.0 (binary bundled + LICENSE/NOTICE). Provisioned by agent-manager. |
| razzfazz-coding-agent-opencode | `modules/agents/coding-agent-web/Dockerfile` | `AGENT_KIND=opencode; ARG OPENCODE_VERSION=1.18.25 (overridden by .env OPENCODE_VERSION)` | https://www.npmjs.com/package/opencode-ai | #36 coding-agent split — per-TYPE image built from the shared modules/agents/coding-agent-web/Dockerfile via AGENT_KIND=opencode. Sandboxed per-user tmux web terminal (:3004). Provisioned by agent-manager. |
| razzfazz-coding-agent-user-defined | `modules/agents/coding-agent-web/Dockerfile` | `AGENT_KIND=user-defined (no bundled coding CLI — user installs their own)` | — | #36 coding-agent split — per-TYPE image built from the shared modules/agents/coding-agent-web/Dockerfile via AGENT_KIND=user-defined. Bare sandbox for a user-supplied coding CLI. Provisioned by agent-manager. |
| razzfazz-coding-tools | `agents/coding-tools/Dockerfile` | `ARG GSD_PI_VERSION=3.0.0 (legacy deprecated gsd-pi pkg), ARG OPENCODE_VERSION=1.18.25 (overridden by .env)` | https://www.npmjs.com/package/gsd-pi | SUPERSEDED (#36) by the per-type coding-agent split (razzfazz-coding-agent-*). Kept buildable for existing provisioned instances. opencode aligned 1.17.5 -> 1.17.13. gsd stays on the OLD deprecated `gsd-pi` pkg (3.0.0, last release) here; the split builder uses the maintained @opengsd/gsd-pi. |
| razzfazz-cognee | `modules/knowledge/cognee/Dockerfile` | `FROM cognee/cognee:1.5.3` | https://github.com/topoteretes/cognee/releases | ladybug graph backend (embedded); adds litellm/tiktoken runtime patches via .pth and vendors the ladybug json extension so cognee needs no egress at build or startup (#79). 2026.08 bump #1: base 1.2.2 -> 1.4.0 (ladybug 0.17.1, extension-release 0.17.0, unchanged from 1.2.2). 2026.08 bump #2 (unfreeze, operator "cognee muss rauf"): 1.4.0 -> 1.5.3. VERIFIED directly against upstream pyproject.toml + GitHub release notes for v1.4.1..v1.5.3: no breaking changes reported anywhere in that range; pgvector client pin (pgvector>=0.3.5,<0.4) unchanged — no cognee-data wipe / re-cognify needed this time (contrast bump #1). DOES cross a ladybug on-disk format boundary: v1.5.0 bumped the ladybug pin from >=0.16.0,<=0.18.2 to ==0.19.0 (Linux); cognee_db_workers.ladybug_migrate.ladybug_version_mapping maps 0.17.1 -> storage code 41 and 0.19.0 -> storage code 43 (verified by fetching that module at v1.5.3), so #186 kuzu-migrate.py's major.minor check correctly detects "migrate" and drives cognee's own ladybug_migration() API — no code change needed, it already does a single EXPORT/IMPORT hop regardless of version gap. Re-vendored the ladybug JSON extension for 0.19.0 (LBUG_EXT_VER=0.19.0; sha256-verified DISTINCT binary from the old 0.17.0 one despite identical 827 KB size — reusing the old file would have served the wrong ABI offline). Retained BOTH prior tiers (vendor/libjson-0.17.0.lbug_extension, vendor/libjson-0.16.0.lbug_extension) so the migration's old-engine EXPORT leg stays offline (#79) for boxes at either prior on-disk format. Migration API confirmed present at cognee v1.5.3. TODO at cut: real build + live migration test (BOX-STEP — not run in this sandbox; no live Cognee available here). |
| razzfazz-cognee-frontend | `modules/knowledge/cognee/frontend/Dockerfile` | `ARG COGNEE_TAG=v1.5.3` | https://github.com/topoteretes/cognee/releases | Next.js UI built from upstream cognee-frontend/ subtree (knowledge/cognee/frontend/Dockerfile). Pin MUST track razzfazz-cognee (same upstream tag) — bump both together. Upstream does not publish a frontend image; we git-clone the tag and build it. rc6.7 #57. 2026.08 bump #1: COGNEE_TAG v1.2.2 -> v1.4.0. 2026.08 bump #2 (unfreeze): v1.4.0 -> v1.5.3 (tracks razzfazz-cognee). Re-verified at v1.5.3: all 9 files patched by the #282 same-origin sed block still exist with the expected `|| "http://localhost:8000"` fallback string; the logo/loading on-disk dirs are still lowercase (no case-sensitivity regression); Next.js ^16.0.8 / React ^19.1.2 pins and the empty upstream next.config.mjs are unchanged from v1.4.0. |
| razzfazz-config | `core/config/Dockerfile` | `python:3.11-alpine` | — | Our code. Configuration portal (internal-tracking). |
| razzfazz-dify-web | `modules/dify/Dockerfile` | `git clone langgenius/dify @ DIFY_VERSION (clone-at-build, #68)` | https://github.com/langgenius/dify/releases | Custom build of Dify web frontend; clone-at-build from the pinned DIFY_VERSION tag (no vendored source). base node:22-alpine. |
| razzfazz-gpustack | `llm/gpustack/Dockerfile.vulkan` | `git clone --branch v0.7.1` | https://github.com/gpustack/gpustack/releases | AMD GPU build; base rocm/rocm-terminal:6.4. |
| razzfazz-gpustack-cuda | `llm/gpustack/Dockerfile.cuda` | `nvidia/cuda:12.8.1-devel-ubuntu22.04 (builder) + git clone --branch b9851 (llama.cpp) onto gpustack/gpustack:v0.7.1` | https://github.com/gpustack/gpustack/releases | NVIDIA/Blackwell build (#946): gpustack 0.7.1 + from-source llama.cpp b9851 for CUDA sm_120. Bases nvidia/cuda:12.8.1-devel-ubuntu22.04 + gpustack/gpustack:v0.7.1 (follow-up #946: digest-pin both bases like Dockerfile.vulkan, captured from the box build). Produces gpustack:cuda (profile llm-legacy with HARDWARE=nvidia, #1448). #894: the builder CUDA generation is load-bearing, not cosmetic — gpustack 0.7.1 bundles llama-box v0.0.171 built on cuda-12.4, whose CUDA ARCHS stop at 900 (no sm_120), so on a Blackwell card it offloads and then crashes on the first llama_decode (verified on an RTX PRO 6000). 12.8 is the first CUDA that emits sm_120. Stay on the 12.x line: the runner is grafted into the 0.7.1 image and links its libcudart.so.12. Guarded by tests/unit/consistency/test_894_blackwell_cuda128.py. |
| razzfazz-help | `core/help/Dockerfile` | `python:3.11-alpine3.24 + monolith 2.10.1-r0 (vendored)` | https://github.com/Y2Z/monolith/releases | Our code, plus ONE vendored third-party binary: the `monolith` single-file web archiver used by the #525/#824 capture path for the six SPA/CDN doc sites (dify, cognee, openhands, gotenberg, komodo, lightrag). Fetched at image-BUILD time against pinned sha256s (DECISION-7c), so air-gap boxes get it inside the image and need no egress for it at run time. NOT the upstream GitHub release asset: `monolith-gnu-linux-*` is glibc-dynamic (PT_INTERP=/lib64/ld-linux-x86-64.so.2, verified 2026-09-02) and cannot execute on the musl base; upstream publishes no static/musl Linux build. Vendored instead: the musl build of the SAME upstream version from Alpine v3.24/community — https://dl-cdn.alpinelinux.org/alpine/v3.24/community/x86_64/monolith-2.10.1-r0.apk sha256 c49865c57faab6614c97f24c913dd7eb5c3f0f64de6b4d04a4418c4365a324c9 (usr/bin/monolith sha256 f5feac97e49f10d5f65aa3f10b0f5fd4aa2129d4fa036e72eff8614eb0332226) and .../aarch64/monolith-2.10.1-r0.apk sha256 df7c985210ebce311b62d6a95b22d1549d3d65bc911e4e587d0f7c34b467f233 (usr/bin/monolith sha256 c57919771a51ed796a03742113c322daf1f0dd7e118f66019ef4bdee8112ee32). RE-PIN RULE: the base tag and all four hashes move together — the binary links this branch's musl/libssl.so.3, and the build refuses (exit 1) when /etc/alpine-release no longer matches ARG ALPINE_BRANCH. Guarded by tests/unit/consistency/test_824_vendored_monolith.py. |
| razzfazz-licenses | `core/licenses/Dockerfile` | `python:3.11-alpine` | — | Our code. |
| razzfazz-llm-manager | `modules/llm/manager/Dockerfile` | `python:3.12-slim + node:24-alpine (swagger-ui build stage) + swagger-ui-dist 5.32.14 (vendored, tarball sha256 609702d791d8d3cdcbc3a52632f6be2f9b743eadf6ba49ca9737dac2a6e0b2a3)` | https://github.com/swagger-api/swagger-ui/releases | Our code. #254 LLM Manager (manager) — FastAPI control plane: auth, enforcement, streaming proxy, TOKEN-ONLY metering (input/output/cached), keys+cost-centers, /metrics, LiteLLM config generation. Phase-1 (2026.09). The paired llm-manager-router service reuses the LiteLLM image pin tracked under images.litellm-mac-gateway (LITELLM_VERSION). SHIP GATE: repin runtime deps (requirements.txt) + the LiteLLM tag at GA. #1195: /docs (Swagger UI) is SELF-HOSTED — the Dockerfile's node:24-alpine `swagger-ui` stage runs `npm pack swagger-ui-dist@5.32.14`, verifies the tarball with `sha256sum -c` against ARG SWAGGER_UI_DIST_SHA256 (DECISION-7c, same pattern as razzfazz-help/monolith; npm integrity of the same file: sha512-nOA2pSQhcmODMUQZpJHYKNuwniDUqcOWGNaSCOoZv12FdOSJ9JxV95HtyRGNMqEBj6h6lCNTy20TgZDYTSuUIg==) and copies six files into app/static/vendor/swagger-ui/ (gitignored — nothing minified in git; offline packages ship the built image). RE-PIN RULE: ARG SWAGGER_UI_DIST_VERSION + ARG SWAGGER_UI_DIST_SHA256 in the Dockerfile and this upstream_pin move together — tests/unit/llm-manager/test_1195_docs_selfhosted.py goes red when they drift — and the per-version CSP audit (no CDN loader, no eval, data: URIs only) is re-run at every bump. |
| razzfazz-llm-manager-ui | `modules/llm/manager-ui/Dockerfile` | `node:24-alpine (build stage) → nginx:1.27-alpine (runtime)` | https://hub.docker.com/_/nginx | Our code (the LLM Manager console, #1444). Untracked here until the 2026.09-ga cut: the release SBOM enumerates the manifest, so an untracked build is invisible to the CVE evidence — this image carried 16 Critical / 64 High from its nginx Alpine base at the cut (#2333, #2336). |
| razzfazz-llm-worker-agent | `modules/llm/node-agent/Dockerfile` | `python:3.12-slim` | https://hub.docker.com/_/python | Our code (the LLM worker agent, #1443). Untracked here until the 2026.09-ga cut; 8 Critical / 25 High from its Debian base with 1 of 33 fixable at the cut (#2336). |
| razzfazz-mcp-cognee-authshim | `modules/mcp-manager/cognee-authshim/Dockerfile` | `FROM cognee/cognee-mcp:1.5.3.dev1` | — | Our code. MCP auth shim fronting the per-user Cognee MCP proxy; built from modules/mcp-manager/cognee-authshim (mcp-manager #36). 2026.08 bump #1: wraps cognee/cognee-mcp:1.4.0. 2026.08 bump #2 (unfreeze): wraps cognee/cognee-mcp:1.5.3 (lockstep with the shared cognee-mcp sidecar and the razzfazz-cognee/-frontend base bump). |
| razzfazz-mcp-manager | `modules/mcp-manager/manager/Dockerfile` | our code | — | Our code. Personal-MCP manager — provisions per-user MCP proxy instances from core/mcp/personal-mcp-catalog.yaml; served at mcp.<domain> (mcp-manager #36). |
| razzfazz-mcp-test-echo | `modules/mcp-manager/test-echo-mcp/Dockerfile` | our code | — | Our code. Minimal echo MCP server used to validate the mcp-manager provisioning path (catalog/test fixture). |
| razzfazz-model-sync | `llm/sync_models/Dockerfile.sync_models` | `python:3.11-slim` | — | Our code. |
| razzfazz-paperclip | `paperclip/Dockerfile` | `ARG PAPERCLIP_TAG=v2026.707.0 (overridden by .env PAPERCLIP_VERSION)` | — | Custom build; git-cloned upstream paperclipai/paperclip at PAPERCLIP_VERSION. |
| razzfazz-stack-hermes-agent | `modules/agents/hermes-agent/Dockerfile` | `ARG HERMES_AGENT_VERSION=v2026.8.27 (overridden by .env HERMES_AGENT_VERSION; this string was already stale at v2026.7.1 == hermes v0.18.0 before this bump, corrected in lockstep)` | https://github.com/NousResearch/hermes-agent/releases | Locally-built per-user Hermes agent image (upstream ghcr.io/nousresearch/hermes-agent tracked as the runtime_only `hermes-agent` pin). #36 Option B single container: built-in v0.18 dashboard (:9119) + gateway (:8642). Provisioned per-user by agent-manager. |
| razzfazz-stack-moltis | `modules/agents/moltis/Dockerfile` | `ARG MOLTIS_VERSION=20260827.01 (overridden by .env MOLTIS_VERSION)` | https://github.com/moltis-org/moltis/releases | Locally-built per-user Moltis agent image (upstream ghcr.io/moltis-org/moltis tracked as the runtime_only `moltis` pin). Root-owned binary; managed update via MOLTIS_VERSION bump + re-provision (#139). Provisioned per-user by agent-manager. |
| razzfazz-start-portal | `core/start-portal/Dockerfile` | `python:3.11-alpine` | — | Our code. internal-tracking start portal — post-login launcher served at start.<domain>; renders tiles based on core/start-portal/manifest.yaml + per-user prefs. |
| razzfazz-wazuh-certs-generator | `apps/wazuh/certs-generator/Dockerfile` | `wazuh/wazuh-certs-generator:0.0.4 + wazuh-certs-tool.sh (packages.wazuh.com/4.8, sha256 acd4fb48c9646904069d81ed9c67f849cf910ab5eb127e051fe9db1dae068874)` | https://github.com/wazuh/wazuh-docker/releases | #1277: thin wrap of upstream's generator that BAKES the cert tool at build time. Upstream's /entrypoint.sh curls wazuh-certs-tool.sh from packages.wazuh.com at CONTAINER START, which makes the wazuh module impossible on an offline box (#184), hands that curl to the corporate proxy on a proxied box (#181), and leaves the validation rule #1259 pinned our SAN format against unpinned. RE-PIN RULE: the URL and the SHA256 build ARGs move together with this entry; tests/unit/consistency/test_1277_wazuh_certs_generator_offline.py goes red when they drift. The wrapper script reproduces upstream's post-download steps (Wazuh Docker, GPLv2 — the tool is fetched at build time, never vendored into this repo). |

_This file is regenerated from the manifest. To bump a version, edit `config/manifests/versions.json`, run `scripts/publish-manifest.sh --regenerate-checksum`, then `scripts/generate-versions-md.py`._
