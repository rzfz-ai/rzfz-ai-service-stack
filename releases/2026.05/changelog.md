# 2026.05 — chronological changelog

Per-rc release notes are retained at `releases/<tag>/` for history. This file is the chronological summary that consolidates the whole cycle.

## v2026.05-rc2 — 2026-04-23 (first RC)

Personal Agent Manager (M011 + M014); managed update channel (M013); Cognee 1.0.1 + embedded Kuzu graph (FalkorDB retired); five security findings closed (F-A2 backup encryption, F-A3/F-B3 socket-proxy, F-B2 pipelines pin, F-B5 CORS template, F-B6 firewall+SSH, F-B7 rate-limits); four new project skills (`check-and-bump-versions`, `security-review`, `security-documentation`, `prepare-release`).

`migrations/env-changes.json`: 59 entries. 34 `add` + 2 `remove` net of internal version bumps.

## v2026.05-rc3 — 2026-04-23 (Authentik two-hop)

Authentik 2025.10 → 2026.2.2 via three new init containers (`authentik-media-migrator`, `authentik-migrate-reconcile`, `authentik-migrate-hop`) — no operator-side migration steps. Same rc2 feature set otherwise.

`migrations/env-changes.json`: 4 entries.

## v2026.05-rc4 — 2026-04-24 (M019 + M020)

**M019 Observability:** new `observability` profile — OpenWebUI Manifold Pipe → Dify apps; OpenLIT OTLP filter pipeline; ClickHouse trace store with first-boot schema initialiser; OpenWebUI seeder sidecar; pre-backup cleanup hook.

**M020 Per-user agents:** Hermes / Moltis / Coding-Tools provisioned per Authentik user (was stack-global). New "My Agents" Authentik drawer; Opencode pipe; Moltis pipe; Hermes agent type with shared `hermes-workspace`. `scripts/migrate-to-per-user-agents.sh` runs from `razzfazz-upgrade.sh` Step 9b — single-admin stacks auto-migrate; multi-user stacks get a yellow banner + runbook.

**Bootstrap script:** `razzfazz-upgrade-from-2026.04-GA.x.sh` (replaces `razzfazz-upgrade.sh` on disk before bash loads it — closes the in-place re-exec gap on 2026.04-ga boxes).

Three image-version `change_default` entries (`DIFY_SANDBOX_VERSION`, `DIFY_PLUGIN_VERSION`, `KOMODO_DB_VERSION`).

`migrations/env-changes.json`: 19 entries.

## v2026.05-rc5 — 2026-04-27 (M018 GPU Stack Unfreeze)

**Single-milestone release.** Kernel + ROCm + GPUStack triple unfrozen:
- Kernel `linux-image-6.14.0-37-generic` (held) → `linux-oem-24.04d` (`6.17.0-1017-oem`, unheld)
- ROCm `6.4.2-120` → `7.2.0` (in-tree amdgpu, no DKMS)
- GPUStack `0.7.1` (custom Vulkan build) → `gpustack/gpustack:v2.1.2` (upstream)

`llm-box` / `llm-experimental` / `llm-cpu` profiles collapsed into a single `llm` profile with a `HARDWARE=amd|nvidia|cpu` selector + per-hardware overlay (`compose.devices.<hw>.yml`). `llm-legacy` retained as M018 rollback safety net (frozen 0.7.1 + Vulkan).

**Custom backends** registered automatically by `llm/gpustack/init-backends.py`:
- AMD Strix Halo: `kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1` (and `llama-vulkan-runner:b8943` from the M022 evaluation)
- CPU: `ghcr.io/ggml-org/llama.cpp:server`
- NVIDIA: GPUStack's built-in vLLM backend

**Stability sidecar:** `autoheal` (willfarrell/autoheal:1.2.0) restarts containers in Docker `unhealthy` state. **Kernel sysctl tunables** (`vm.panic_on_oom=2`, `kernel.panic=10`, `kernel.panic_on_oops=1`, `vm.swappiness=10`) installed by init.sh / upgrade.sh — replaces the silent host hang from S03.1 with a 10-second-delayed reboot.

**M022 monkey-patches** (`llm/gpustack/usercustomize.py`): scheduler dispatch (`is_gguf_model`), GGUF dashboard sizing (`get_model_weight_size`), GPU-utilisation sysfs fallback for amdsmi-on-gfx1151. All degrade to no-op when upstream lands fixes.

`migrations/env-changes.json`: 8 entries (drops `GPUSTACK_EXPERIMENTAL_VERSION`, sets `GPUSTACK_VERSION=v2.1.2`, adds `HARDWARE` + `COMPOSE_FILE`).

## v2026.05-rc5.1 — 2026-04-29 (rc5 security-finding mitigation batch)

Closes 5 of 7 F-RC5 audit findings:
- F-RC5-1 (gpustack admin UI LAN-exposed) — closed via `GPUSTACK_BIND` / `GPUSTACK_HOST_BIND` split
- F-RC5-2 (patch bumps don't propagate) — closed via 3 `change_default` rules + `prepare-release.sh` validator
- F-RC5-3 — re-classified misdiagnosis (services use `:ro`, not RW)
- F-RC5-4 — orphan ad-hoc container removed
- F-RC5-5 (sysctl tunables not installed) — closed via fatal-on-fail + sudo-prime in `razzfazz-upgrade.sh`
- F-RC5-6 — *attempted* mitigation (gpustack `-cpu` slim variant; turned out the variant doesn't exist on v2.x — see rc6.5)
- F-RC5-7 — open, deferred to M025-A2

`migrations/env-changes.json`: 4 entries (3 image bumps + `GPUSTACK_HOST_BIND` add).

## v2026.05-rc6 — 2026-04-30 (M023 — feature-complete RC)

**`add-module` skill comprehensively rewritten (M023-S01):** 21 canonical touchpoints (was 7), bakes in security defaults from S01.2, gates emit on validators per (γ+δ) FAIL/WARN policy. End-to-end validated by adding `crawl4ai` clean.

**Image bump set (M023-S02):** Dify `1.13.3` → `1.14.0` GA; dify-plugin-daemon `0.5.8-local` → `0.6.0-local`; paperless-ngx `2.20.15` (CVE fix); Synapse `v1.152.0`; Vaultwarden `1.35.8`; Infisical `v0.146.0-postgres` → `v0.159.23` (image-variant change — `-postgres` discontinued upstream); docling-serve-cpu `v1.17.0`; element-web `v1.12.16`; stirling-pdf `2.10.0`; onyx `v3.2.12` (lockstep); searxng `2026.4.29`.

**New module: `crawl4ai` (M023-S03)** — RAG-friendly web crawler (Apache-2.0, port 8201). Pairs with searxng (URL discovery) for Dify / LightRAG / Cognee / Onyx knowledge bases.

**Repository restructure (M023-S00):** modules now under `apps/`, `knowledge/`, `doc-processing/`, `search/`. Per-rc release notes moved to `releases/<tag>/`. Operator-facing scripts unaffected.

**M020 deprecation cleanup (M023-S06):** removed legacy global `hermes` / `moltis` / `coding-tools` profile remnants.

**Mutable-tag hygiene:** `hermes-agent` / `moltis` per-user catalog pins moved from `:latest` → immutable (`v2026.4.23` / `20260429.01`); coding-tools npm packages pinned via Dockerfile ARG.

`migrations/env-changes.json`: 65 entries (4 image-version `change_default`, 56 Dify 1.14 `.env.dify` `add`, 5 net-new `.env` `add` for `OPENHANDS_VERSION` / `GSD_PI_VERSION` / `OPENCODE_VERSION` / `CRAWL4AI_*`).

## v2026.05-rc6.1 — 2026-04-30 (hot-fix: migrate_env version-compare regex)

**`razzfazz-upgrade.sh` `migrate_env` version-compare regex didn't accept the `rcN.M` form.** `2026.05-rc5.1` parsed via the generic split-by-dot fallback as `(2026, 5, 5, 1)` — incorrectly GREATER than `2026.05-rc6`'s `(2026, 5, 0, 6)`. The rc6 entry's 65 env_changes were silently skipped on rc5.1 → rc6 upgrades; boxes ended up with stale image tags and a Caddy crash loop on missing `CRAWL4AI_DOMAIN`. Fix: rewrote `compare_versions` / `sort_key` as two explicit regexes producing a uniform 5-tuple `(year, month, channel, n1, n2)` with `channel: 0=rc, 1=ga`. Test: `tests/test-version-compare.sh` (16 cases).

`migrations/env-changes.json`: 0 entries (script-only).

## v2026.05-rc6.2 — 2026-04-30 (hot-fix: migrate_env edge cases)

**Two more `migrate_env` bugs surfaced during the M023-S05.2 big-bang test on box-002.**

1. **URL colon split:** bash `IFS=':' read` in the apply loop split URL-shaped `default` values on `://`. `CREATORS_PLATFORM_API_URL=https://creators.dify.ai` landed in `.env` as just `https`, crashing Dify's pydantic validator. Fix: switch field separator to U+001F (ASCII Unit Separator).
2. **Inline comment in `read_env_value`:** values like `KOMODO_DB_VERSION=17-0.106.0-ferretdb-2.5.0 # ferretdb-postgres` (the `.env.example` shape) returned the value+comment verbatim, so `change_default` rules never matched. Fix: strip a trailing whitespace-prefixed `#` comment from unquoted values.

Test: `tests/test-migrate-env-edge-cases.sh` (6 cases).

`migrations/env-changes.json`: 0 entries (script-only).

## v2026.05-rc6.3 — 2026-04-30 (hot-fix: bootstrap workflow)

**Bootstrap fetched onto a 2026.04-ga box leaves itself as untracked.** The target tag has the bootstrap tracked, so `razzfazz-upgrade.sh`'s `git checkout <tag>` aborted with "untracked working tree files would be overwritten by checkout".

Fix #1: bootstrap removes itself (`rm -f "${BASH_SOURCE[0]}"`) before `exec`'ing `razzfazz-upgrade.sh`. Bash has the source loaded into memory; the upcoming `git checkout` materialises it back from the target tag.
Fix #2 (defense-in-depth): `code_update_git` switched to `git stash push -u` (`--include-untracked`) so any other operator-added file that collides also stashes.

`migrations/env-changes.json`: 0 entries (script-only).

## v2026.05-rc6.4 — 2026-04-30 (hot-fix: post-install /etc/hosts + --refresh + guard)

**`razzfazz-post-install.sh setup_local_dns` was hardcoded.** Modules added between releases (notably `crawl4ai`) didn't get `/etc/hosts` entries on selfsigned/local deployments. Fix: derive the FQDN list from `.env`'s `*_DOMAIN` keys at runtime; new modules pick up automatically.

**New `--refresh` mode** — idempotent post-upgrade re-config: runs ONLY `setup_local_dns` + `ensure_gpustack_api_key`. Doesn't touch models, plugins, or operator-set defaults. This is what the upgrade script's new post-upgrade reminder block points operators at.

**`--preset` destructive-state guard** — refuses to run on a previously-initialised stack without `--force`, after surfacing what would be overwritten (GPUStack models, Open WebUI / Dify / Gitea defaults, plugin selections).

**Upgrade reminder block** added to the end of `razzfazz-upgrade.sh` — guides operators to `--refresh` without auto-calling post-install.

`migrations/env-changes.json`: 0 entries (script-only).

## v2026.05-rc6.5 — 2026-04-30 (hot-fix: gpustack CPU image tag)

**`llm/compose.devices.cpu.yml` appended `-cpu` to `GPUSTACK_VERSION`** → `gpustack/gpustack:v2.1.2-cpu`, a tag that doesn't exist on Docker Hub. CPU upgrades aborted at `docker compose pull` with `manifest unknown`. Surfaced during a big-bang upgrade test on a CPU-only test environment.

Root cause: gpustack v2.x ships a single unified image; the v0.7.1-era pattern of per-hardware variants (`-cpu`, `-rocm`) was discontinued upstream. The F-RC5-6 attempted slim-CPU mitigation in rc5.1 was based on an upstream variant that never shipped for v2.x.

Fix: drop the image override; CPU inherits the unified `gpustack/gpustack:${GPUSTACK_VERSION:-v2.1.2}` from `llm/compose.yml`, identical to AMD/NVIDIA. F-RC5-6 stays open as **accept-residual**.

`migrations/env-changes.json`: 0 entries (`requires_pull: true`; post-upgrade `docker compose pull` picks up the corrected tag).

## v2026.05-rc6.6 — 2026-04-30 (hot-fix: Step 5b legacy cleanup + status.sh)

**Two follow-ups from the rc6.5-era CPU-only test pass.**

1. **`migrate_llm_profiles` (Step 5b)** — three services in `llm/compose.yml` share `container_name: gpustack` (`gpustack-legacy` / `gpustack` / `gpustack-cpu`), mutually exclusive by container_name. Flipping `COMPOSE_PROFILES` from `llm-cpu` → `llm` left the legacy `gpustack-cpu` running under the shared name; the next `docker compose up -d` died with `Error response from daemon: Conflict. The container name "/gpustack" is already in use`. `--remove-orphans` doesn't help (legacy services are still defined, just inactive). Fix: Step 5b explicitly stops + `rm -f` the legacy services right after the `.env` edit.

2. **`razzfazz-status.sh` false-FAIL** — the script flagged the rc6.5-correct `gpustack/gpustack:v2.1.2` on a CPU host as wrong. Updated check: pass on `gpustack/gpustack:v2.<patch>`, fail only when an operator manually pinned a non-existent v2.x hardware-suffixed tag.

`migrations/env-changes.json`: 0 entries (script-only).

## v2026.05-rc6.7 — 2026-05-04 (44+ numbered hot-fix items)

The "stabilisation" rc — a long stream of operator-driven hot-fix items uncovered during pre-GA shake-down. The rc6.7 series numbers items 1-94 internally; the headline batch:

- **#43–#46 OpenHands sandbox runtime** — per-conversation sandbox runtime in host-network mode; Cognee container port moved from `8000` to `8011` to free `:8000` for the sandbox's hardcoded port.
- **#48 gpustack v2.x stop/start memory leak** — Patch 4 estimator + bus.py + cache.py upstream backport (PR #5255 staged surgically); F-RC5-6 b mitigated, F-RC5-6 accept-residual.
- **#52–#54 Backup management UI** — async create button, dashboard, checksum history.
- **#56 Hermes + Moltis built from upstream source** — immutable provenance.
- **#57 Cognee Next.js frontend** — Caddy splits routing (`/api/*` to FastAPI backend, everything else to the new frontend container).
- **#61–#62 Open WebUI ↔ GPUStack auth unified** on a single `GPUSTACK_API_KEY`; the legacy `GPUSTACK_OPENAI_API_KEY` is dropped from `.env.example` (the compose-side ollama-proxy rename completes in rc6.9).
- **#63 BLOCKER AMD GPU device access** — `/dev/dri/renderD128` + `/dev/kfd` were unreadable inside the gpustack container (image's `render` group at gid 109; host's at gid 992). Fix: pass host's render gid as a numeric supplementary group at compose-time. Pre-fix every fresh AMD install hung at model deploy with `ACCEL_WORKING -13 EACCES`. New `RENDER_GID` env added to `.env`.
- **#64 BLOCKER backend registration order** — `init-backends.py` now runs *before* model deploy in `razzfazz-post-install.sh` so the first deploy never stalls on "no backend versions available for GPU device".
- **#67–#69 Cognee onboarding bypass + proxy-headers**; `init-backends.py` exec-bit gating (the file ships at mode 0644 and is invoked via `python3`, so the executable bit is meaningless — earlier the gate used `-x` and the whole block was silently skipped on every fresh install).
- **#70 Cognee cognify works on a fresh container** without manual onboarding.
- **#77, #82 Paperclip** — data dir chown at provisioning, auto-onboard on first boot, invitation URL surfaced in container logs; `gsd-pi 2.78.1 → 2.80.0` + opencode npm dependency pin.
- **#89, #90 OpenHands** — sandbox path-proxy through the parent container; sandbox URL env corrected to upstream-canonical `SANDBOX_CONTAINER_URL_PATTERN`.
- **#92 OpenHands / Hermes** target `/v1-openai` (was the GPUStack-native `/v1`).
- **#93 Per-user agent idle auto-stop** disabled by default (operator can re-enable per user).
- **#94 + followup Profile-toggle policy bindings** re-applied after each toggle so per-app SSO group bindings don't drift.

`migrations/env-changes.json`: 1 entry (rc6.7 — `COGNEE_PORT` 8000 → 8011 default + `RENDER_GID` add).

## v2026.05-rc6.8 — 2026-05-03 (cognee-frontend integration tag)

Dedicated tag bundling the rc6.7 #57 cognee-frontend module + the rc6.7 #63 AMD GPU access fix into a single migrate-env entry for operators upgrading after the long rc6.7 hot-fix run. New env vars: `COGNEE_FRONTEND_PORT` (default `8012`), `RENDER_GID` (default `992`).

`migrations/env-changes.json`: 2 entries (`COGNEE_FRONTEND_PORT` add + `RENDER_GID` add).

## v2026.05-rc6.9 — 2026-05-08 (LLM Runtime stability repositioning + toggle hardening + F-RC5-1)

**Three blocks of fixes landed this rc:**

**A. M029-S04 LLM Runtime stability repositioning.** A 12-hour stop/start dual-model soak on production AMD Strix Halo hardware (2460 iterations / 99.92% HTTP 200 / RSS started 218 MB and ended 167 MB) validated v0.7.1 leak-free under sustained load. The customer-facing default flips back to v0.7.1 — `llm-legacy` (AMD) and `llm-cpu` (CPU) become the new STABLE defaults. The unified `llm` profile (v2.1.x) stays as opt-in EXPERIMENTAL via `--llm-experimental` at install or via the new Configuration Portal panel.

**B. LLM Runtime toggle in the Configuration Portal — 13 distinct fixes** caught during end-to-end exercising (both directions, twice each):

1. Wire-shape: `action` vs `action_type` on the apply-action submit; SSE envelope is `{type, text}` not `{line, done}`; audit_logger `event` positional was passed as a kwarg.
2. UX: confirmation dialog needed `margin: auto` to centre; active runtime card was visually less prominent than the inactive one.
3. Container ops: `docker stop` left the shared `container_name` slot held by the stopped container, blocking the next `up`; orphan v2.x runner pods (`gemma4-…-run-0`, etc., spawned via the docker socket with `restart: unless-stopped`) survived `compose down` and held worker ports `40000-40063`; `gpustack_db` alembic schemas are incompatible across v0.7.1 ↔ v2.x and need a drop+recreate on cross-version flip.
4. Compose scope: `up -d --force-recreate` against the whole project recreated `docker-socket-proxy` mid-flight; razzfazz-config talks to docker via that proxy, so the in-flight subprocess lost its connection. Fix: scope the up to the LLM services only.
5. Post-flip bootstrap: invoke `razzfazz-post-install.sh --refresh` to re-mint `GPUSTACK_API_KEY` against the fresh DB and re-register custom backends when entering the v2.x path.
6. Image gaps: `razzfazz-config` runs `python:3.11-alpine` which has no `bash` or `curl`; both added.
7. `--refresh` itself: skip `init-backends.py` (which targets `/v2/inference-backends`) when the active profile is `llm-legacy` / `llm-cpu` (the v0.7.1 path doesn't have v2 backend endpoints — would 404).

**C. F-RC5-1 closure.** GPUStack worker ports `10150-10151` were unconditionally bound on `0.0.0.0` (master-mode design value) regardless of `GPUSTACK_MODE`. The 2026-04-30 audit incorrectly listed F-RC5-1 closed; the rc5.1 fix only covered `GPUSTACK_HOST_BIND` (port 9090). rc6.9 closes the symmetric leak by writing both `GPUSTACK_HOST_BIND` and `GPUSTACK_WORKER_HOST_BIND` per `GPUSTACK_MODE` from `razzfazz-init.sh`, and adds a new `migrate_gpustack_bind` step in `razzfazz-upgrade.sh` to converge existing boxes. Standalone → loopback, master → all-interfaces, worker → host-loopback / worker-all-interfaces. Both `llm` and `llm-legacy` profiles inherit the same default, harmonising hardening across the runtime versions.

**D. Smaller fixes.**

- `START_DOMAIN` (default `start.${MAIN_DOMAIN}`) was added to `.env.example` mid-cycle for the new Start Portal but never registered in the migration manifest; pre-rc6.9 upgrades hit a Caddy crash loop because `{$START_DOMAIN} {` expanded to a bare `{`. rc6.9 registers the add-action.
- `COMPOSE_FILE=` empty-value bug: docker compose 2.40+ tries to read the project root as a file when `COMPOSE_FILE` is set to an empty string. Both `razzfazz-init.sh` and `razzfazz-upgrade.sh`'s `migrate_llm_profiles` now write `COMPOSE_FILE=compose.yml` (not empty) for legacy / cpu profile paths.
- `razzfazz-init.sh` HARDWARE setter previously only ran for the `llm` profile, leaving `HARDWARE=amd` (the .env.example default) on `llm-cpu` / `llm-legacy` presets. Setter is now extended to all three LLM profile names, with mode-appropriate `COMPOSE_FILE` writes.
- `vm.panic_on_oom` flips from `1` (panic-and-reboot) to `0` (kill-largest-process) — proved too aggressive on tight-memory boxes during pre-GA load testing.
- `GPUSTACK_OPENAI_API_KEY` → `GPUSTACK_API_KEY` rename completed on the compose-side: `ollama-proxy` now reads the canonical key everything else uses.

`migrations/env-changes.json`: 1 entry (rc6.9 — `START_DOMAIN` add).

## v2026.05-ga — 2026-05-08

Consolidated GA tag. See `releases/2026.05/RELEASE_NOTES.md` for the full release notes spanning rc2..rc6.10. Validated on three boxes: the development environment, a single-box test environment (Strix Halo), a CPU-only test environment. `razzfazz-status.sh` clean (19 PASS / 2 WARN / 0 FAIL on prod-class).

`migrations/env-changes.json`: 1 entry (ga — `GPUSTACK_OPENAI_API_KEY` remove, completing the rename started in rc6.7 #61/#62).

## post-v2026.05-ga hotfix series — 2026-05-09 → 2026-05-11 (subsequently tagged as v2026.05-ga.1)

15 commits accumulated against `main` between the GA tag at `8b45c1b` and the kassasturz overnight cycle 2026-05-11. None are blocking; each is independently safe to deploy. Tagged as `v2026.05-ga.1` (see next section).

**Stability — Strix Halo runtime path.**
- `fix(llm/gpustack): remove autoheal=true label` across all three variants (`gpustack-legacy`, `gpustack`, `gpustack-cpu`). Autoheal was kicking during model first-load on Strix Halo because the storm exceeds Docker's default unhealthy window (3 retries × 60s). The kill triggered runner reload → another storm → cascade. 100-min stress-validated (4-stream parallel inference, 26 472/26 485 = 99.95% HTTP 200, zero restarts).
- `fix(llm): relax gpustack healthcheck for Strix Halo storm window` — `start_period: 300s`, `retries: 10`. Companion to autoheal removal.
- `fix(post-install): default restart_on_error=False for deployed models` — both v0.7.x and v2.x deploy paths in `razzfazz-post-install.sh` now POST `'restart_on_error': False`. A partially failed first load no longer loops infinitely re-loading and re-triggering storms.
- `fix(gpustack:vulkan): bump bundled llama.cpp b8639 → b9101` — adds gemma4 / qwen35moe / mistral3 architecture support for the legacy Vulkan path. (BuildKit cache trap: `--no-cache` doesn't always invalidate buildx cache mounts; needed `docker buildx prune -af` to actually pick up the new tarball — documented in memory.)

**Configuration UI / autoprovisioner — close cross-container drift.**
- `fix(config): autofill cognee/lightrag LLM_MODEL + EMBEDDING_MODEL on profile-enable` — previously empty defaults caused `/api/v1/add` to time out at the LLM connection test (30s) on every upload. Standard preset deploys `gemma4` + `nomic-embed-text`; the autoprovisioner now back-fills these. Cognee gets `openai/gemma4` (litellm `<provider>/<model>` requirement); lightrag gets bare `gemma4` (uses OpenAI SDK directly).
- `chore(release): housekeeping` — bundle commit `44448ead`:
  - **CADDY_IP retirement.** Caddy now declares `${AUTHENTIK_DOMAIN}` and `${MATRIX_DOMAIN}` as Docker network aliases on the default bridge. Removes the static-IP `extra_hosts` plumbing that broke vaultwarden / matrix every time Caddy got a new bridge IP. `CADDY_IP` env var, `_detect_caddy_ip()` helper, and the `CADDY_IP_DEPENDENT` set are gone; migration removes the var on next upgrade.
  - **Profile autoprovisioner gaps closed.** Added `LIGHTRAG_API_KEY`, `COGNEE_ADMIN_PASSWORD`, `GITEA_INTERNAL_TOKEN`. New USER-aware `*_DB_PASSWORD` generator for paperless / infisical / onyx / gitea (was being skipped — the inline rationale was wrong for the common .env-from-template case, leaving the DSN as `<user>:<POSTGRES_PASSWORD>` and failing auth).
  - **`postgres-db-reconcile` dependency** added to cognee, lightrag, paperless-ngx, gitea — these had only `postgres: service_healthy` and would crashloop on profile-enable-after-install.
  - **Help docs refresh** — searxng (no UI; was misadvertised), stirling-pdf domain (`pdf.<domain>` not `stirling-pdf.<domain>`), getting-started (added 6 missing rows in /etc/hosts + DNS + navigation), llm.md (EXPERIMENTAL banner per M029-S04), cognee/lightrag/vaultwarden authentication sections.
  - **EMBEDDING_MODEL drift fixed** — `.env.example` + `profile_provisioner.py` had `nomic-embed-text-v1.5` but `razzfazz-post-install.sh` actually deploys as bare `nomic-embed-text`. Embedding lookups would 404 on every fresh install.
  - **New skill: kassasturz.** End-to-end overnight house-keeping orchestrator at `.claude/skills/kassasturz/SKILL.md`.

**Per-user agent surface — fix three things at once.**
- `fix(agent-manager): correct branding URL paths` — templates referenced `/branding/media/razzfazz-ai-logo.png` but the file lives at `/srv/authentik-media/media/public/razzfazz-ai-logo.png` (Caddy `branding_static` strips `/branding`, no extra `media/`). Logo now serves; favicon now serves.
- `feat(agent-manager): on-demand TLS + opaque-token subdomains` — wildcard `*.agents.<domain>` LE cert was impossible via HTTP-01 (only DNS-01 works for wildcards) and we ship no DNS-01 plugin. Replaced with on-demand TLS — Caddy issues per-name HTTP-01 certs at first request. New global `on_demand_tls { ask http://agent-manager:5000/api/tls/ask }` guard validates each hostname maps to a registered instance before issuance, blocking cert-flood DoS. Same commit replaces `{type}-{user_slug}.agents.<domain>` with `{type}-{token}.agents.<domain>`, where `token` is HMAC-SHA256(instance_uuid, AGENT_DOMAIN_TOKEN_SECRET) truncated to 8 hex chars — username never appears in DNS or TLS SNI metadata. Backwards-compat: legacy user_slug subdomains stay reachable until the instance is recreated.

**Vaultwarden SSO.**
- `fix(vaultwarden): trust upstream IdP email verification` — `SSO_ALLOW_UNKNOWN_USER_EMAIL_VERIFICATION=true`. Authentik's default OIDC `email` scope mapping does not propagate `email_verified=true` from federated Google upstream; vaultwarden saw `false` and bounced login. Trust at the vaultwarden boundary; future operator follow-up: extend Authentik's mapping to propagate the upstream claim.

**Smaller fixes.**
- `fix(start-portal): ship Sortable.min.js` (was excluded by `.gitignore`).
- `fix(hermes-workspace): pin pnpm to v9` (v10 strict-build broke lockfile).
- `fix(apply_manager): recreate start-portal on every profile toggle`.
- `fix(openwebui): disable in-app update-available banner by default` (`ENABLE_VERSION_UPDATE_CHECK=false` in `.env.example`).

`migrations/env-changes.json`: 1 entry (`2026.05-ga.1` — `CADDY_IP` remove).

## v2026.05-ga.1 — 2026-05-13

The 15-commit hotfix series above plus the overnight 2026-05-12/13 session work:

- **LLM single source of truth (S0).** `core/llm/standard-models.yaml` + `core/llm/sync.py` + the `propagate-llm-config` operator skill. Reconciles into GPUStack model definitions, OWUI connections, Dify model providers, Cognee/LightRAG/Onyx configs, and the per-user coding-tools / hermes / moltis instances. Consumer-side adoption (catalog.py / coding-tools entrypoint / post-install / the last 5 consumers) is deferred to ga.3.
- **Patched llama.cpp Vulkan build.** `llm/gpustack/patched-llama-cpp/` produces a custom llama.cpp build (b9112 + `ggml-org/llama.cpp#22458`) that fixes the gemma4 second-image SWA prompt-cache crash on AMD Strix Halo. Build is reproducible from `build.sh`; binaries are not committed (`.gitignore`).
- **M031 follow-up source fixes (7).** Bake in the lessons from the overnight session: agent-manager `provisioner.upgrade()` self-heals on partial-failed state (A1); catalog.py `SANDBOX_CONTAINER_URL_PATTERN` + `PAPERCLIP_PUBLIC_URL` use `{{instance_hash}}` instead of `{{user_slug}}` to track the on-demand TLS opaque-token migration (A3); agents that need root for one step `docker exec -u uid` correctly for the rest (A4); OpenHands gets its own subdomain instead of overloading openhands-akadmin (B1); paperclip auto-registers its allowed-hostname list on first start so fresh instances don't 403 (B2); Caddy entrypoint cleans up stale ACME locks (C1) and self-heals when a `.key` file goes missing (C2).
- **OpenWebUI** `0.9.4 → 0.9.5` (sec/bug fixes per upstream).
- **gotenberg** `8.31.0 → 8.32.0` (8.31 crash-loops on `--chromium-deny-private-ips`).
- **Dify SMTP 1.14 fix.** `SMTP_LOCAL_HOSTNAME=dify-api` (was empty → postfix HELO failed only on Dify 1.14).
- **Manifest drift cleanup.** `manifests/versions.json` resyncs to shipped reality (gotenberg 8.32.0, komodo 2.2.0, valkey 9.0.4, searxng 2026.5.9); komodo-periphery moved out of `hardcoded` to `images` to stop double-counting; OPENWEBUI_VERSION `0.9.4 → 0.9.5` migration added (drift caught during the test environment upgrade run).

`migrations/env-changes.json` entry for `2026.05-ga.1`: `CADDY_IP` remove, `ENABLE_VERSION_UPDATE_CHECK` add (default `false`), `OPENWEBUI_VERSION` change_default `0.9.4 → 0.9.5`.

## v2026.05-ga.2 — 2026-05-13

Focused Dify password-reset usability patch.

- **Persist upstream Dify `_TokenData` phase fix.** Dify 1.14 silently drops the `phase` field from reset tokens because PR #34380's `_TokenData` TypedDict doesn't list it, breaking PR #35425's security gate (`GHSA-4q3w-q5mc-45rq`) and 400-ing every password reset. Filed upstream as [`langgenius/dify#36116`](https://github.com/langgenius/dify/issues/36116) with the one-line fix in [`langgenius/dify#36117`](https://github.com/langgenius/dify/pull/36117). Until that lands, a small entrypoint wrapper at `dify/patches/entrypoint-wrapper.sh` adds the missing field at container start (idempotent grep guard, no-ops once upstream merges).
- **Reset-token expiry.** `RESET_PASSWORD_TOKEN_EXPIRY_MINUTES` bumped `5 → 30`. Upstream default doesn't survive real-world email delivery.
- **Login lockout duration.** New `LOGIN_LOCKOUT_DURATION=1800` (30 min). Upstream default is 86400 (24 h) which is unfriendly to fat-finger sequences. `LOGIN_MAX_ERROR_LIMITS=5` stays at the upstream-hardcoded class attribute.

`migrations/env-changes.json` entry for `2026.05-ga.2`: `RESET_PASSWORD_TOKEN_EXPIRY_MINUTES` change_default `5 → 30`, `LOGIN_LOCKOUT_DURATION` add (default `1800`).

## v2026.05-ga.3 — 2026-05-13

The cycle-closing patch (re-opened 4 days later by ga.4 for an Authentik CVE bundle — see below).

- **M030 S4** — per-user agent named volumes integrated into `razzfazz-backup.sh`. Operator-visible: a backup now includes per-user agent state (previously skipped, despite the named volumes existing since GA). `--skip-agents` opt-out for size-constrained snapshots.
- **M031 S1–S4 fully** — every LLM-config consumer reads `standard-models.yaml` directly: `catalog.py`, coding-tools entrypoint templated from env, `razzfazz-post-install.sh` invokes `sync.py`, plus opencode / hermes / moltis / gpustack model deploy / lightrag / cognee adoption commits. Closes the qwen3.5 / qwen3.6 drift surfaced during fleet validation.
- **Security review (Mode A) + documentation refresh.** `security-run/razzfazz-ai-box-security-assessment-v2026.05-ga.3.md` audit artifact. `docs/security-architecture.md` rewritten across 10 sections; customer handover doc bugs fixed (no longer punts security review to customer; bootstrap-password rotation properly documented).
- **Pre-tag tripwire** + **strengthened pre-flight gates.** `scripts/pre-tag-check.sh` blocks any `v*-ga*` tag whose diff touches the auth-relevant surface without a fresh `security-run/*-<tag>.md` artifact. `.git/hooks/pre-push` enforces this even when `prepare-release.sh` is bypassed. CLAUDE.md adds a hard rule that all GA-line tags must go through the `release-cycle` skill.
- **In-cycle bumps for the ga.3 cut:** `GSD_PI_VERSION` 2.80.0 → 2.82.0, `OPENCODE_VERSION` 1.14.42 → 1.14.46, Dify 1.14.0 → 1.14.1 (patch — security fix to SECRET_KEY bootstrap + dependency sweep).
- **LLM_ARGS env added** so Cognee passes thinking-mode-off + adequate max_tokens to LiteLLM (closes the 30s hang on cognee pre-flight).

`migrations/env-changes.json` entry for `2026.05-ga.3`: 15 env_changes (mostly Dify 1.14 upstream-key surfacing, plus the GSD_PI / OPENCODE bumps).

## v2026.05-ga.4 — 2026-05-17

Security-driven patch — late-cycle add to address upstream CVEs that landed after `ga.3`. (Originally intended as the cycle's final patch; `ga.5` subsequently extended the line — see below.)

- **Authentik 2026.2.2 → 2026.2.3** — fixes 1 CRITICAL (reflected XSS in SFE) + 4 HIGH advisories. The headline finding is **GHSA-5wcc** — an unauthenticated forward-auth bypass via the `X-Original-URI` header — which directly applies to our Caddy `forward_auth` topology (every razzfazz.ai box). The hardcoded `authentik-init` migration-hop image moves 2025.12.4 → 2025.12.5 in lockstep (1 HIGH GHSA-h6x7).
- **PostgreSQL CVE-2026-2003/4/5/6** (three at CVSS 8.8). Floating `postgres:17` tag now resolves to 17.10 on `docker compose pull`. `pgvector/pgvector` pinned from floating `:pg17` to immutable `:0.8.2-pg17` for reproducibility.
- **ClickHouse observability hotfix** — `mem_limit` raised 2 GiB → 6 GiB + `merge_tree` backpressure tuning. Discovered during prod investigation 2026-05-17: ClickHouse was OOMing under its own `system.text_log` / `system.asynchronous_metric_log` merge backlog (engine self-logging churn, not application telemetry).
- **Broad upstream sweep — 18 image bumps.** OpenHands 1.6.0 → 1.7.0 (compose + sandbox runtime + per-user catalog). Per-user agent images: hermes-agent v2026.5.7 → v2026.5.16, hermes-workspace v2.1.3 → v2.3.0, moltis 20260510.01 → 20260517.01. Paperclip v2026.428.0 → v2026.513.0. opencode 1.14.46 → 1.15.3. Cognee 1.0.9 → 1.1.0. Infisical v0.159.28 (no bump — upstream v0.159.29 reported but does not exist on Docker Hub; v0.159.28 IS the latest stable; deferred). element-web v1.12.17 → v1.12.18. Vespa 8.671.12 → 8.687.75. SearXNG 2026.5.9 → 2026.5.17.
- **edge-tts hygiene pin** — was unpinned in our custom image; pinned to `==7.2.8`.
- **Drift fixes** — valkey compose-default 9.0.3 → 9.0.4, lightrag compose-default v1.4.15 → v1.4.16, manifest runtime_only entries synced (moltis, hermes-workspace, openhands-runtime).
- **M032 closure** — test foundation milestone reached 0/0/0 (1114 passed / 0 fail / 0 error / 0 skip / 111 xfail) on the test environment after a long bug-fix campaign + 3-layer ephemeral-postgres leak fix + LLM-variant cycling with restore + wait-for-healthy + BUG-6 cascade-password fix in `core/init-db.sh`.

Deferred to M033 (housekeeping cycle): ClickHouse 24.8.4.13 → 26.x LTS, gsd-pi 2.82.0 → 3.0.0 (breaking major), kyuz0/amd-strix-halo-toolboxes rocm-7.2.1 → 7.2.3 (Strix Halo sensitivity), OpenWebUI 0.9.5 (2 HIGH GHSAs OPEN upstream — track only), **OpenLIT observability pipeline non-functional end-to-end (M033-S11)** — discovered during the ClickHouse hotfix investigation; every `openlit_*` table is empty or seed-only; no spans land; root cause at writer side (OWUI pipelines / Dify OTel SDK / GPUStack instrumentation).

`migrations/env-changes.json` entry for `2026.05-ga.4`: 5 `change_default` entries (AUTHENTIK 2026.2.2 → 2026.2.3, SEARXNG 2026.5.9 → 2026.5.17, PAPERCLIP v2026.428.0 → v2026.513.0, OPENHANDS 1.6.0 → 1.7.0, OPENCODE 1.14.46 → 1.15.3). `requires_build: true`. `requires_pull: true`. No breaking changes. No new env keys.

## v2026.05-ga.5 — 2026-05-25

Feature + infrastructure patch (M035 + M034 + M033). The cycle's largest post-GA addition.

- **Stack-wide MCP registry (M035)** — `core/mcp/mcp-servers.yaml` + sync engine wires MCP servers into every consumer. First server: **Cognee memory** (`remember`/`recall`/`forget`), reachable from Open WebUI (native streamable-HTTP MCP), Dify (proxy-free plugin daemon — SSRF isolation intact), the per-user Moltis/Hermes/OpenCode agents, and an OpenHands `config.toml` emitter. New `cognee-mcp` sidecar (gated by the `cognee` profile).
- **Automated 24.04 → 26.04 LTS host migration (M034)** — `scripts/razzfazz-upgrade-os.sh` (non-interactive dist-upgrade + post-reboot `--reconcile`), `docs/upgrade-guide-26.04-lts.md`, Secure-Boot verification in init, Strix Halo GRUB tunables, mainline-kernel script deprecated.
- **Security bumps** — Caddy 2.11 → 2.11.3 (CVE-2026-30851 forward_auth identity-injection), Dify 1.14.1 → 1.14.2 (CVE-2026-41949 cross-tenant authz bypass + CVE-2026-41948 plugin-daemon path traversal), Gitea 1.26.1 → 1.26.2 (May round). Gotenberg CVE-2026-42589/42596 evaluated, not bumped (no fixed upstream tag).
- **~15 field-bug fixes (M033)** — model names from `standard-models.yaml`; Dify admin-email + fail-loud on zero models; harden-host stack-dir-owner detect; build-on-upgrade for new services; LightRAG/Cognee admin-pw alignment; help center ships both GPUStack v0.7+v2.1 docs; OpenHands sandbox env; checksum journal_mode; Authentik MFA policy-binding for 2026.2.x.
- **Shared-lib consolidation (M033)** — agent-manager → `razzfazz_common.auth`; AuditLogger promoted into `razzfazz_common.audit_log` (config re-exports).
- **Append-only GA-tag drift protection** — GA tags immutable; `razzfazz-status.sh` flags deployed-commit vs tag drift.

`migrations/env-changes.json` entry for `2026.05-ga.5`: two `change_default` entries (DIFY_VERSION 1.14.1→1.14.2, GITEA_VERSION 1.26.1→1.26.2) so existing `.env` files adopt the patched pins on upgrade, plus one `add` (COGNEE_MCP_API_KEY, empty — minted post-install). `requires_build: true` (Caddy custom image). `requires_pull: true` (Dify/Gitea). No breaking changes.

## v2026.05-ga.6 — 2026-05-29

Code/configuration patch; no third-party image bumps. **SSO: Google + Microsoft Entra ID concurrent login** — per-provider login-flow blueprints overwrote each other's identification-stage source list (enabling Entra dropped Google); now generated as one combined blueprint, Authentik init version → 2.9. **Scoped default-closed Dify→doc-tools SSRF allow-list** (`DIFY_DOC_TOOLS_SSRF_ALLOW`, defaults to non-resolving `disabled.invalid`); R-NET-10 guardrail test tightened to permit only that sanctioned rule. **Configuration Portal hardening** — image-update scoping, `--no-deps` module toggles, service-name translation + env-revert on failed update, Gitea-API manifest update-check, single-box GPU dedupe. **Post-install** Authentik outpost + MCP reconcile on `--refresh`. presidio-analyzer autoheal; dify-sandbox PYTHON_PATH fix; docling German OCR; Start-Portal forward-auth `/outpost.goauthentik.io/*` handler.

`migrations/env-changes.json` entry for `2026.05-ga.6`: one `add` (`DIFY_DOC_TOOLS_SSRF_ALLOW=disabled.invalid`, closed default — MUST NOT be empty, else Caddy crash-loops on a bare `allow`). `requires_build: true` (custom-image source changed). `requires_pull: false`. No breaking changes, no data migration. Mode A security review: no exploitable web-exposure findings; image CVE surface unchanged (no new images).

## v2026.05-ga.7 — 2026-06-01

Backup & restore disaster-recovery fix; no third-party image bumps, no env changes. **Backup pre-hook restored** — the DB-dump + encrypted-`.env` + per-user-agent-tarball hook silently stopped running when the backup service moved to `offen/docker-volume-backup` v2 (v2 dropped the v1 `EXEC_PRE_BACKUP` env var); re-wired via the supported `docker-volume-backup.archive-pre` container label. Every backup taken between the v2 bump and this release captured volumes only — **no Postgres dump, `.env`, or agent state** — so those archives are not restorable; operators should take a fresh backup after upgrading. **Restore onto a clean install fixed** — restore now recreates services so they re-read the restored `.env` (was `docker start`, which kept stale credentials → universal `password authentication failed` crash-loop after a boundary-crossing restore), replaces volume contents instead of merging a backup over an existing PGDATA, preserves absolute symlinks (`media -> /media` was being rewritten into a self-loop that broke `authentik-media-migrator`), and reads per-user agent tarballs from the correct `databases/agents` path. **New `backup-restore` suite scenario** exercises the full seed → backup → wipe → reinit → restore disaster-recovery cycle across every stateful service (validated 5/5 on the test box), so an incomplete backup or broken restore now fails loudly in CI.

`migrations/env-changes.json` entry for `2026.05-ga.7`: none (no environment changes). `requires_build: true` (`razzfazz-backup-management` custom-image source changed). `requires_pull: false`. No breaking changes, no data migration. Mode A security review: component/version surface unchanged from ga.6 (no image bumps); backup archives remain GPG-encrypted at rest and now contain the database dump + encrypted env files they were always meant to.

After ga.7 ships, the `2026.05-ga` line continues; the next cycle is `2026.06`.
