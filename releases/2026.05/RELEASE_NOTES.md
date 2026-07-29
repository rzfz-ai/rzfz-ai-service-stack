# Release notes — v2026.05

**Cycle window:** 2026-04-23 → 2026-05-25 (cycle extended by the `v2026.05-ga.5` feature + infrastructure patch).
**GA tag:** `v2026.05-ga` — released 2026-05-08.
**Latest patch:** `v2026.05-ga.7` — released 2026-06-01.
**Patch line:** [`v2026.05-ga.1`](#patch-releases) (2026-05-13) — Caddy network-alias refactor, LLM-config single-source, agent-provisioning follow-up source fixes, patched llama.cpp build for Strix Halo, OpenWebUI 0.9.4 → 0.9.5, gotenberg 8.31 → 8.32. [`v2026.05-ga.2`](#patch-releases) (2026-05-13) — Dify password-reset usability fixes. [`v2026.05-ga.3`](#patch-releases) (2026-05-13) — M031 S1-S4 single-YAML LLM config rollout + M030 S4 per-user agent state in backups + cycle-closing security-review + handover doc rewrite. [`v2026.05-ga.4`](#patch-releases) (2026-05-17) — security-driven patch: Authentik CRITICAL XSS + GHSA-5wcc forward-auth bypass + PostgreSQL CVE coverage + broad upstream sweep of 18 third-party images + ClickHouse observability OOM hotfix. [`v2026.05-ga.5`](#patch-releases) (2026-05-25) — stack-wide MCP registry (Cognee memory across OWUI/Dify/Moltis/Hermes/OpenCode + OpenHands emitter), automated 24.04→26.04 LTS host-migration tooling, ~15 field-bug fixes (M033), shared-lib consolidation, security bumps (Caddy 2.11.3, Dify 1.14.2, Gitea 1.26.2). [`v2026.05-ga.6`](#patch-releases) (2026-05-29) — Google + Microsoft Entra ID concurrent SSO login, scoped default-closed Dify→document-tools SSRF allow-list, Configuration Portal module/version-management hardening, post-install Authentik outpost reconcile. [`v2026.05-ga.7`](#patch-releases) (2026-06-01) — backup & restore disaster-recovery fix: the backup pre-hook silently stopped running on the offen v2 backup engine (backups were missing the database dump, encrypted `.env`, and agent state); re-wired via the `archive-pre` label, fixed restore to recreate services with the restored secrets and preserve absolute symlinks, plus a new `backup-restore` DR test scenario. **Take a fresh backup after upgrading — pre-ga.7 backups are not restorable.**

## Executive summary

The 2026.05 cycle is the largest 2026.x release to date. The headline change is the **personal agents go per-user** rework (operators provision Hermes / Moltis / Coding Tools / OpenHands / Paperclip from a "My Agents" Authentik drawer) plus a brand-new **razzfazz.ai Start Portal** at `start.<domain>` as the operator's stack-wide tile-based landing page with per-user pinning, drag-and-drop reorder, and custom categories. **Authentik jumps two majors** (2025.10 → 2026.2.3) entirely from Docker Compose with no operator-side schema steps. **Dify lands its 1.14 line** (1.13.3 → 1.14.1) with the LiteLLM 1.83 supply-chain fix. The **host-side kernel + ROCm freeze is broken** — operators can now run modern OEM kernels (`6.17`+) and ROCm 7.2 — but **the GPUStack runtime itself stays on v0.7.1 + custom Vulkan as the recommended STABLE default** for AMD and CPU. Upstream GPUStack v2.1.x is added as opt-in EXPERIMENTAL for operators who specifically need its NVIDIA / vLLM / kyuz0 ROCm support; a 12-hour stop/start soak on production AMD Strix Halo hardware showed v2.1.x leaking under sustained load while v0.7.1 stayed flat, which drove the late-cycle decision to keep v0.7.1 as the recommended path.

A new module — **Crawl4AI** — joins the search profile alongside SearXNG, completing the URL-discovery → content-fetch pipeline for downstream RAG into LightRAG / Cognee / Onyx / Dify. The **Management UI** gains a stack-wide observability profile (OpenLIT + ClickHouse), a new **OpenWebUI ↔ Dify Manifold Pipe** lets operators route any chat conversation through a Dify workflow, and Cognee's bare FastAPI page is replaced with the **upstream Next.js frontend** built locally from source. The Configuration Portal gains a **Modules → LLM Runtime** panel that flips the runtime between v0.7.1 and v2.1.x in one click — handling profile flip, container teardown, orphan runner pod cleanup, schema reset, API-key rotation, and custom-backend re-registration inline.

The customer upgrade path from 2026.04-ga is a single command: fetch the bootstrap script and run `./razzfazz-upgrade-from-2026.04-GA.x.sh --target v2026.05-ga.4`. After GA, four patch tags ship within the same line — `v2026.05-ga.1` (Caddy network-alias refactor + LLM single-source-of-truth + agent-provisioning hardening), `v2026.05-ga.2` (Dify password-reset usability), `v2026.05-ga.3` (single-YAML LLM-config rollout + per-user agent state in backups + cycle-closing security review), and `v2026.05-ga.4` (security-driven patch: Authentik 2026.2.3 + GHSA-5wcc forward-auth bypass + PostgreSQL CVE coverage + broad upstream sweep of 18 third-party images + ClickHouse observability OOM hotfix). All four are reachable by a normal `razzfazz-upgrade.sh --target <tag>` from any ga box. Six of seven post-upgrade audit findings closed by GA — including the late-cycle re-discovery and full closure of **F-RC5-1** (gpustack worker ports were still bound on `0.0.0.0` in standalone mode despite earlier patches; the GA cut makes the bind mode-aware in both `razzfazz-init.sh` and a new `migrate_gpustack_bind` step in `razzfazz-upgrade.sh`). The remaining open items (gpustack v2.x bundled-deps CVE inheritance, autoheal Alpine base) are documented as accept-residual / planned-fix and do not block ga.

---

## Table of contents

- [Cross-cutting changes](#cross-cutting-changes) — the things that span multiple modules across GA and the patch line
- [Module-by-module changes](#module-by-module-changes) — every module, alphabetical within each category
  - [Identity & Access](#identity--access)
  - [AI Workflows & Chat](#ai-workflows--chat)
  - [LLM Inference](#llm-inference)
  - [Knowledge & RAG](#knowledge--rag)
  - [Document Processing](#document-processing)
  - [Search & Crawl](#search--crawl)
  - [Collaboration & Vault](#collaboration--vault)
  - [Personal Agents](#personal-agents)
  - [Observability](#observability)
  - [Operations](#operations)
- [Operator UX — Management UI improvements](#operator-ux--management-ui-improvements)
- [Upgrade instructions](#upgrade-instructions)
- [Security](#security)
- [Known issues at GA](#known-issues-at-ga)
- [Patch releases](#patch-releases) — ga.1 and ga.2 summaries

---

## Cross-cutting changes

These touch multiple modules and are best understood at the cycle level. Module-specific detail is in the per-module sections below.

### Late-GA maintenance sweep (in-window updates)

The cycle's GA window closed with a maintenance sweep that bundles 16 image bumps + a Gotenberg SSRF flag re-enable + an Authentik edge mitigation + a Dify XSS compensating control + the dify-web 1.11.2 → 1.14.0 monorepo re-vendor + the cognee 1.0.1 → 1.0.9 graph-driver swap + the Hermes-Agent v2026.5.7 ("Tenacity") rebuild. Validated end-to-end on the development and test environments with both fresh-install and bootstrap-upgrade-from-2026.04-ga.6 paths. Five real bugs were caught and fixed in current main during the validation cycle (force-rebuild on commit-move; fail-fast on non-interactive sudo; bootstrap fetch refspec; outpost-attach race; outpost drift). All carried in `v2026.05-ga`. The post-ga patch line subsequently bumped Cognee 1.0.9 → 1.1.0 and Hermes-Agent v2026.5.7 → v2026.5.16 (full-cycle pins reflected in the per-module sections below).

#### Authentik edge mitigation for GHSA-qvxx-mfm6-626f

Upstream advisory ([GHSA-qvxx-mfm6-626f](https://github.com/goauthentik/authentik/security/advisories/GHSA-qvxx-mfm6-626f), CVSS 9.1, Authenticated RCE in Policy/Property Mapping test endpoint) is unpatched on the 2026.2.x line. Authentik shipped patches on `2025.8.6 / 2025.10.4 / 2025.12.4`; no 2026.2.x patch published as of GA. RBAC analysis confirmed only `akadmin` (superuser) holds the relevant `view_policy` / `view_propertymapping` perms in our default deployment, so the practical attack surface is operator-credential compromise only. Caddy edge block layered on top:

```caddy
@ghsa_qvxx path_regexp ghsa ^/api/v3/(policies|propertymappings)/all/[^/]+/test/?$
respond @ghsa_qvxx 403
```

Lives inside the `{$AUTHENTIK_DOMAIN}` site block in `core/Caddy/Caddyfile`. Removable by commenting two lines once 2026.2.3 lands. Operators who legitimately need to invoke the test endpoint from the Authentik admin UI can comment the block temporarily.

#### Dify echarts XSS — Content-Security-Policy-Report-Only

Upstream advisory ([GHSA-qqjx-5h5w-x5vj](https://github.com/advisories/GHSA-qqjx-5h5w-x5vj), echarts DOM XSS) has no upstream patch. Compensating control on `dify.<domain>`: `Content-Security-Policy-Report-Only` header with `default-src 'self'` + permissive `script-src` for the observation phase (Next.js 16 hydration relies on inline scripts), violation reports POSTed to `/csp-report` → Caddy `respond 204`. Operators tune the policy from observed reports and flip the header name from `Content-Security-Policy-Report-Only` to `Content-Security-Policy` (enforcing) once the report stream is quiet.

#### Authentik App library slim-down via `blank://blank`

The mid-window first attempt at slimming the Authentik App library down to "start + help + licenses only" deleted the per-module `authentik_core.application` rows and broke forward-auth — Authentik's proxy outpost requires `application__isnull=False` on every gated host. Reverted same-day. Redo (currently shipped): set `meta_launch_url: "blank://blank"` on every per-module Application; Authentik's frontend `appHasLaunchUrl` filter then hides the tile from the user library while the Application + ProxyProvider stay live for the outpost. Documented upstream as the official workaround. The previously per-profile blueprint-state-gating machinery retires — apps now stay always-present; profile gating moves entirely into the start-portal manifest.

#### dify-web 1.11.2 → 1.14.0 monorepo re-vendor

Upstream restructured `web/` into a true pnpm monorepo (`web/` + `e2e/` + `sdks/nodejs-client/` + `packages/`) with `workspace:*` deps. We re-vendored from upstream tarball; ported our 6-line build-arg Dockerfile delta (`NEXT_PUBLIC_*` ARG/ENV passthrough) into upstream's new monorepo Dockerfile. Build context still `dify/web/`; `dockerfile:` field updated to `web/Dockerfile`. Closes the 3-minor split between dify-api/worker/sandbox and dify-web. _Note: the vendored `dify/web/package.json` still carries upstream's own version string (`1.11.2`) — that's an upstream `web/package.json` field, not the version of Dify our image runs against. The image is built and shipped against the cycle's `DIFY_VERSION` (1.14.0 at GA, 1.14.1 from `ga.3` onward)._

#### cognee 1.0.1 → 1.1.0 (full cycle)

The GA cut moved 1.0.1 → 1.0.9; `ga.4` carries the line on to **1.1.0** (upstream skipped 1.0.10 entirely). The kuzu (`==0.11.3`) → ladybug (`==0.16.0`) graph-DB driver swap that landed in 1.0.9 carries forward in 1.1.0. Bumped after operator confirmed no production data on the development and test environments; cognee-data + cognee-data-storage volumes were emptied in-place via an alpine helper container (the `docker volume rm` path is blocked because backup-service + razzfazz-backup-management hold the volume mounts). Our local Dockerfile patch from rc6.7 #70 (`OpenAICompatibleEmbeddingEngine` constructor missing `self.max_completion_tokens` / `self.tokenizer`) **retired** — upstream 1.0.9 sets both natively in the constructor + uses a richer `get_tokenizer()` selection that our hardcoded `_TT70(...)` would have regressed. The `.pth` runtime hook (litellm + tiktoken model→encoding map) stays.

#### Hermes-Agent v2026.4.23 → v2026.5.16 (full cycle)

GA landed on v2026.5.7 ("Tenacity Release") — 8 P0s closed upstream including a CVSS 8.1 Discord allowlist scoping fix and TOCTOU auth fixes. `ga.4` carries forward to v2026.5.16 to pick up subsequent upstream patches. Dockerfile changed substantially in the GA cut: new `ui-tui/` workspace (Terminal UI on ink), the internal `hermes-ink` package referenced as a `file:` workspace dep that has to be COPY'd in full before `npm install` runs, `ENV npm_config_install_links=false` to force npm 9 to symlink `file:` deps the way npm 10+ does, tini PID-1 wrapper to reap orphaned MCP stdio subprocesses (`tini -g -- /opt/hermes/docker/entrypoint.sh`). All ported into our build pipeline at `agents/hermes-agent/Dockerfile`. Per-user image; new provisions on a fresh `ga.4` install land on v2026.5.16, in-flight instances stay on whatever tag they were provisioned against until users re-provision.

#### Upgrade-script + bootstrap fixes (5 bugs caught during validation)

- **`razzfazz-upgrade.sh`** now force-sets `REQUIRES_BUILD=true` whenever `INSTALLED_COMMIT != TARGET_COMMIT`, so any commit-moving upgrade rebuilds local-built images regardless of whether the migration manifest says `needs_build`. Closes a silent-stale-code path.
- **`razzfazz-upgrade.sh`** detects missing tty (`! -t 0 || ! -t 1`) before `sudo -v` and exits with an actionable message instead of hanging on the password prompt under `nohup`.
- **`razzfazz-upgrade-from-2026.04-GA.x.sh`** now uses `refs/tags/${TAG}:refs/tags/${TAG}` for the bundle fetch (was `${TAG}:${TAG}` which git interprets as `refs/heads/`, refusing to write annotated-tag objects there).
- **`apply-policy-bindings.py`** polls `ProxyProvider.objects.count()` until two consecutive 15-second windows show stable count before attaching. Fixes the race with Authentik's blueprint controller.
- **`apply-policy-bindings.py`** verify-and-reattach loop: after the initial attach, sleep 20s, refresh-from-db, re-attach any provider that got detached by Authentik's `outpost_controller` task. Up to 3 rounds (~60s ceiling).

`INIT_VERSION` 2.4 → 2.7 forces every upgraded box to re-run init-authentik.sh + apply-policy-bindings.py.

### Host unpinning: kernel + ROCm freed, runtime stays on v0.7.1 stable

The cycle unblocks the host-side stack from the kernel-6.14 / ROCm-6.4 freeze that had kept previous releases locked to a single old kernel. Operators can now move freely up the OEM kernel security train. The GPUStack runtime itself stays on the validated **v0.7.1 + custom AMD Vulkan build** as the recommended STABLE default — upstream **v2.1.x is added as opt-in EXPERIMENTAL** for operators who specifically need its NVIDIA / vLLM / kyuz0 ROCm support, but it is **not** recommended for production AMD or CPU installs after the late-cycle leak observation (see *LLM runtime stability repositioning* below).

| Layer | 2026.04-ga | 2026.05 STABLE recommended | 2026.05 EXPERIMENTAL opt-in |
|---|---|---|---|
| Kernel (standard hardware) | `6.14.0-37-generic` (held) | `linux-oem-24.04d` meta-package (unheld; tracks the OEM train) | same |
| Kernel (AMD Strix Halo gfx1151) | `6.14.0-37-generic` (held) | mainline `6.18.x` + `ttm.pages_limit=` / `ttm.page_pool_size=` GRUB params | same |
| ROCm | `6.4.2-120` (DKMS) | `7.2.0` (in-tree `amdgpu`, `--no-dkms`) | same |
| GPUStack | `0.7.1` + custom AMD Vulkan build | **`0.7.1` + custom AMD Vulkan build (unchanged)** | `gpustack/gpustack:v2.1.2` (upstream unified) |

> **Two kernel paths in 2026.05.** On **standard hardware** (CPU installs, NVIDIA, single-box AMD without Strix Halo `gfx1151`) the recommended path is the `linux-oem-24.04d` meta-package — Canonical updates it as new OEM-supported point releases land. On **AMD Strix Halo `gfx1151`** the recommended path is the **mainline-builds `6.18.x`** kernel installed alongside, because the OEM `6.17` line's in-tree `amdgpu` silently ignores `amdgpu.gtt_size`; without `ttm.pages_limit=` + `ttm.page_pool_size=` GRUB params on `6.18`+, large-model load triggers a kworker D-state pile-up (load average grows into the hundreds). Operators of Strix Halo boxes should set those GRUB params at install time. Rollback to any prior kernel still works via the GRUB submenu.

The host-side kernel hold from previous cycles existed because ROCm 6.4's DKMS module wouldn't compile against newer kernels — that coupling is broken by ROCm 7.2's `--no-dkms` install (uses the in-tree `amdgpu` driver). Once the host de-couples, both runtime paths (v0.7.1 STABLE and v2.1.x EXPERIMENTAL) are simultaneously available; operators no longer have to choose between "stay on the old runtime" and "stay on the old kernel".

**Net change for STABLE operators on AMD or CPU:** the runtime image is the same as 2026.04-ga (`0.7.1` Vulkan build for AMD, `gpustack/gpustack:v0.7.1-cpu` for CPU). The kernel and ROCm versions on the host are new, but the LLM behaviour is unchanged. NVIDIA installs land on the EXPERIMENTAL `llm` profile because no v0.7.1 NVIDIA build exists upstream — same as in 2026.04-ga.

**Profile additions.** A new `llm` profile (v2.x) is added alongside the existing `llm-legacy` (AMD v0.7.1) and `llm-cpu` (CPU v0.7.1-cpu) profiles. The trio is mutually exclusive by `container_name: gpustack`, so only one runs at a time. `razzfazz-upgrade.sh` Step 5b migrates the legacy `llm-box` / `llm-experimental` profile names from 2026.04-ga to `llm-legacy` automatically.

**Custom backends** (only registered when the EXPERIMENTAL `llm` profile is active):

- `llama-box-vulkan-custom` — local image (llama.cpp `b8943` + Mesa RADV); operator-buildable AMD path.
- `llama-box-rocm-custom` — `kyuz0/amd-strix-halo-toolboxes:rocm-7.2.1`; AMD alternative.
- `llama-box-cpu-custom` — `ghcr.io/ggml-org/llama.cpp:server`; CPU.
- NVIDIA uses GPUStack's built-in `vllm` backend (no custom registration needed).

**Endurance validated on STABLE.** Production 12-hour stop/start dual-model soak on AMD Strix Halo: 2460 iterations / 99.92% HTTP 200 / RSS started 218 MB and ended 167 MB / latency flat across the full run. The validation run that drove the late-cycle decision to keep v0.7.1 as the recommended default.

### Personal agents go per-user

Hermes / Moltis / Coding Tools / OpenHands / Paperclip are now **per-Authentik-user**, not stack-global. Each user provisions their own from the new "**My Agents**" drawer in the Authentik portal. Per-user subdomain routing (`<type>-<user_slug>.agents.<domain>`); global quotas configurable from Management UI → Settings → Personal Agents.

The legacy global `hermes` / `moltis` / `coding-tools` profiles are removed. `scripts/migrate-to-per-user-agents.sh` runs from `razzfazz-upgrade.sh` Step 9b — single-admin stacks auto-migrate global volumes; multi-user stacks see a yellow banner and `docs/migration-2026.05-per-user-agents.md`.

### Authentik major-version jump

Migrated end-to-end through three new init containers (`authentik-media-migrator`, `authentik-migrate-reconcile`, `authentik-migrate-hop`) — bridges the 2025.12 schema migration boundary entirely from compose with no operator-side steps. Existing sessions and SSO configurations preserved. Google OAuth customers: `docs/authentik-upgrade.md` for the preservation desk review.

### OpenWebUI ↔ Dify Manifold Pipe (new feature)

Route any OpenWebUI chat conversation through a Dify workflow. Configure via `DIFY_APPS_JSON` in `.env`:

```json
[
  {"id":"<pipe_id>","name":"Display name","api_key":"app-...","type":"chat|workflow|completion"}
]
```

Each entry exposes a model-named pipe in OpenWebUI's model selector. Populate from existing Dify apps via `./dify/seed-apps.sh`.

### Repository structure

Modules now live under `apps/`, `knowledge/`, `doc-processing/`, `search/` (was repo root). Per-rc release notes at `releases/<tag>/`. Operator-facing scripts unchanged. Driven by the rewrite of the `add-module` skill, which standardises the 21 canonical touchpoints for adding a module and gates emit on `scripts/validate-module.sh`.

### LLM runtime stability repositioning

Earlier in the cycle the new `llm` profile (`gpustack/gpustack:v2.1.2` + custom backends) was on track to become the default — the host-side kernel + ROCm changes had landed and v2.1.x was the obvious follow-on. End-of-cycle 12-hour stop/start soak testing on production AMD Strix Halo hardware showed v0.7.1 holding up cleanly while v2.1.x exhibited a slow server-side memory leak (separate from the bus.py / cache.py upstream PR #5255 already mitigated). The customer-facing decision for AMD and CPU is therefore to **stay on v0.7.1 as STABLE** — `llm-legacy` becomes the recommended STABLE default for AMD, and `llm-cpu` becomes the recommended STABLE default for CPU. The unified `llm` profile (v2.1.x + custom backends) is available as opt-in EXPERIMENTAL via `razzfazz-init.sh --llm-experimental` at install time or via a one-click toggle in the Configuration Portal at any later point. NVIDIA installs continue on v2.1.x unconditionally because no v0.7.1 NVIDIA build exists upstream.

A new **LLM Runtime** panel under Configuration Portal → Modules surfaces the toggle. Switching directions stops + removes the active gpustack and model-sync containers (sharing the `container_name: gpustack` slot across mutually-exclusive profiles), removes orphan v2.x runner pods that hold worker ports `40000-40063`, drops + recreates `gpustack_db` (alembic schemas are incompatible across v0.7.1 ↔ v2.x), brings up only the two LLM services in the new profile (so `docker-socket-proxy` and the rest of the stack are untouched), and finishes by invoking `razzfazz-post-install.sh --refresh` to re-mint `GPUSTACK_API_KEY` and re-register the v2.x custom backends when entering the experimental path. Progress streams live to the operator. The dialog warns that loaded model definitions do not auto-migrate — re-deploy whatever models you want under the new runtime from the GPUStack UI.

### razzfazz.ai Start Portal

A new tile-based landing page at `start.<your-domain>`. Each enabled module gets a tile showing its country-of-origin flag, a one-line description from the manifest, current container memory usage, and a click-through to the underlying app. Per-user pinning (heart icon) sticks favourite tiles to the top of the user's view, drag-and-drop reorder works across categories, and operators can rename, reorder, add, and delete custom categories per user. Multi-column layout adapts to viewport; tile glyph falls back to a custom SVG when an `app_icon` is absent. Authentik forward_auth gates access; a small SQLite database in the container holds per-user preferences.

### Caddy network-alias refactor (ga.1)

The `CADDY_IP` variable + `extra_hosts` workaround that vaultwarden and matrix/synapse used to reach `auth.<domain>` and `matrix.<domain>` from inside the docker bridge is retired in `v2026.05-ga.1`. The Caddy container now carries `${AUTHENTIK_DOMAIN}` and `${MATRIX_DOMAIN}` as Docker network aliases (declared in `core/compose.yml` `networks.default.aliases`); internal services resolve them via Docker's embedded DNS, which survives a Caddy container recreation without operator intervention. Removes a chronic break-on-restart hazard — every Caddy cert restart had previously left vaultwarden + synapse pointing at a stale CADDY_IP with broken OIDC until the operator ran `razzfazz-upgrade.sh --force-reexec` or re-detected the IP by hand.

### Single source of truth for LLM configurations (ga.1)

`core/llm/standard-models.yaml` becomes the one place where LLM identities live (alias, quant, ctx-size, parallel). `core/llm/sync.py` is an idempotent reconciler that pushes changes to every consumer (GPUStack registration, OWUI connections, Dify model providers, Cognee/LightRAG/Onyx config, and the per-user coding-tools / hermes / moltis instances). A new operator skill — `propagate-llm-config` — documents the canonical 9-touchpoint propagation order so a change in one place stays consistent everywhere. The remaining consumer-side rewiring (catalog.py / coding-tools entrypoint / post-install reconciler / the last 5 consumers) lands in `v2026.05-ga.3`.

### Dify password-reset usability and `_TokenData` upstream regression (ga.2)

Dify 1.14 shipped two PRs that interact badly: a security gate that requires a `phase` field on reset tokens (`langgenius/dify#35425`, `GHSA-4q3w-q5mc-45rq`), and a Pydantic refactor that silently drops that exact field on read (`langgenius/dify#34380`). Net effect on every Dify 1.14.x install: every password reset returns `400 invalid_or_expired_token` even when the token is fresh and valid. `v2026.05-ga.2` ships a small entrypoint wrapper (`dify/patches/entrypoint-wrapper.sh`) that adds `phase: str` to the `_TokenData` TypedDict at container start; the patch is idempotent and becomes a no-op once a future Dify release carries the upstream fix. Filed upstream as [`langgenius/dify#36116`](https://github.com/langgenius/dify/issues/36116) with the one-line fix in [`langgenius/dify#36117`](https://github.com/langgenius/dify/pull/36117) (open). Reset-token expiry is also bumped 5 → 30 minutes (upstream default doesn't survive real-world email delivery latency), and login lockout duration is softened from 24 hours to 30 minutes.

---

## Module-by-module changes

Each module section uses the same shape: a metadata table at the top (current → new version, profile, license), then **What's new**, **Security**, and **Migration** subsections (the latter two only when there's something to say). All bullets are intentionally one sentence each.

### Identity & Access

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Authentik

| Version | Profile | License |
|---|---|---|
| `2025.10.x → 2026.2.3` | core (always-on) | MIT (Apache-2.0 modules) |

**What's new in 2026.05**

- Two-major schema upgrade is fully driven from compose via three new init containers; operator runs nothing extra.
- Sanitized overlay at `core/Authentik/migration-overrides/0056_user_roles.py` works around the upstream RunPython bug at the 2025.12 schema boundary.
- New "My Agents" portal drawer surfaces every user's per-user agent instances.
- Patch line `ga.4`: bumped 2026.2.2 → 2026.2.3 to close 1 CRITICAL (reflected XSS in SFE) + 4 HIGH advisories; the headline finding is **GHSA-5wcc**, an unauthenticated forward-auth bypass via the `X-Original-URI` header that directly applies to our Caddy `forward_auth` topology (every razzfazz.ai box). The hardcoded `authentik-init` migration-hop image moves 2025.12.4 → 2025.12.5 in lockstep (1 HIGH GHSA-h6x7). The GA-window Caddy edge-block for GHSA-qvxx-mfm6-626f remains in place — that advisory is still unpatched on the 2026.2.x line as of 2026.2.3.

**Migration**

- Google OAuth customers should re-read `docs/authentik-upgrade.md` for the SSO-binding preservation review (no breaking change, but the rebind step is verified on every upgrade).

---

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Caddy

| Version | Profile | License |
|---|---|---|
| Custom build (rolling) | core (always-on) | Apache-2.0 |

**What's new in 2026.05**

- Rebuilt with the `caddy-ratelimit` plugin so login paths on Authentik and Gitea now have rate-limits applied at the edge.
- New routes added for `crawl4ai`, `observability`, and the per-user agent subdomains.
- Docling and stirling-pdf use `<sub>.{$MAIN_DOMAIN}` directly (no per-module `*_DOMAIN` env override needed).
- Patch line `ga.1`: `${AUTHENTIK_DOMAIN}` and `${MATRIX_DOMAIN}` are now registered as Docker network aliases on the caddy container; vaultwarden's OIDC discovery and synapse's self-callback resolve them via Docker's embedded DNS and survive Caddy restarts without operator intervention. Retires the `CADDY_IP` variable and the `extra_hosts` / `getent hosts caddy` workarounds across vaultwarden, matrix, and `razzfazz-init.sh`.
- Patch line `ga.1`: a stale-ACME-lock cleanup and a broken-cert self-heal land in the Caddy entrypoint, so a previous failed ACME attempt or a missing `.key` file no longer block subsequent issuance attempts.

**Security**

- Login rate-limits close the brute-force exposure window from earlier cycles.

**Migration**

- `migrate_env` in `ga.1` removes `CADDY_IP` from `.env` automatically — the variable is no longer read by anything.

---

### AI Workflows & Chat

#### <img src="/branding/media/razzfazz-ai_chat_icon.png" width="20" height="20"> Open WebUI

| Version | Profile | License |
|---|---|---|
| `→ 0.9.5` | `chat` | BSD-3-Clause (Modified) |

**What's new in 2026.05**

- New manifold pipe entries appear in the model selector when `DIFY_APPS_JSON` is populated, routing chat conversations through any Dify app.
- Function/tool/pipeline seeds drop into the data volume on first run via the new `openwebui-seed` sidecar.
- OpenAI-compatible auth unified on a single `GPUSTACK_API_KEY` env so chat, image generation, embeddings, and reranking all use the same credential (the legacy `GPUSTACK_OPENAI_API_KEY` was retired from `.env.example` and the compose-side rename of `ollama-proxy` to read the canonical key landed in rc6.9).
- Patch line `ga.1`: bumped 0.9.4 → 0.9.5 (sec/bug fixes per upstream).

**Security**

- 0.9.1 CVE backlog carries forward as accept-residual; tracked on the upstream patch watchlist (no fix path available yet).
- 0.9.5 has 2 HIGH GHSAs OPEN with no upstream fix yet; tracked, deferred to M033.

---

#### <img src="/branding/media/razzfazz-ai_workflow_icon.png" width="20" height="20"> Dify

| Version | Profile | License |
|---|---|---|
| `1.13.3 → 1.14.1` | `dify` | Apache-2.0 |

Sub-images bumped together with the main Dify image:

| Sub-image | Version |
|---|---|
| dify-plugin-daemon | `0.5.8-local → 0.6.0-local` |
| dify-sandbox | rolled forward across the cycle |
| dify-web | rolled forward (custom build) |

**What's new in 2026.05**

- Lands Dify 1.14.0 GA after a 2.5-month upstream rc soak; `ga.3` carries the line on to 1.14.1 (upstream patch — security fix to SECRET_KEY bootstrap + dependency sweep).
- 56 new `.env.dify` keys reflect upstream's 1.14 surface (archive storage, Hologres / Baidu vector backends, creators platform, collaboration mode, Redis pool/retry/socket tuning) — all opt-in or tuning knobs, defaults match upstream.
- Code-node outbound HTTP routes through the SSRF proxy as a designed isolation control (sandbox).
- Patch line `ga.2`: small entrypoint wrapper persists the `_TokenData` phase-field fix for the upstream password-reset regression; idempotent grep guard makes it a no-op once Dify ships the upstream fix.
- Patch line `ga.2`: reset-token expiry bumped 5 → 30 minutes, login lockout duration set to 30 minutes (Dify's 24-hour upstream default is unfriendly to fat-finger sequences).
- Patch line `ga.1`: `SMTP_LOCAL_HOSTNAME=dify-api` set explicitly — empty value breaks postfix HELO on Dify 1.14 (1.13 fell back to `socket.gethostname()`; 1.14 passes the empty string literally and the relay returns `501 Syntax: HELO hostname`).

**Security**

- LiteLLM `1.82.x → 1.83.0` closes CVE-2026-35030 and exits the supply-chain malware exposure window of 1.82.7 / 1.82.8.
- Trivy scan: 7 Critical / 90 High → **4 Critical / 75 High** on `dify-api` after the bump.
- CORS wildcard removed from `.env.dify` template.
- The 1.14 `_TokenData phase`-strip regression is purely a usability bug — the upstream security gate it cripples (`GHSA-4q3w-q5mc-45rq`, phase-bound change-email flow) is intact in our patch; we preserve the field rather than dropping the check.

**Migration**

- `migrate_env` adds the 56 new `.env.dify` keys automatically on upgrade.
- `PUBSUB_REDIS_*` → `EVENT_BUS_REDIS_*` rename: both kept in `.env.dify.example` for backward compatibility (Dify 1.14 still reads the legacy aliases).
- `dify-plugin-daemon` PG auth fixed (was authing as `dify_user` against `dify_plugin_db`); pre-fix manual workaround no longer needed.
- `RESET_PASSWORD_TOKEN_EXPIRY_MINUTES` 5 → 30 and `LOGIN_LOCKOUT_DURATION=1800` ship as `change_default` / `add` entries in the `ga.2` migration manifest — fresh installs get the new defaults; existing operators converge on next `razzfazz-upgrade.sh`.

---

### LLM Inference

#### <img src="/branding/media/razzfazz-ai_llm_icon.png" width="20" height="20"> GPUStack (EXPERIMENTAL — v2.1.x)

| Version | Profile | License |
|---|---|---|
| `0.7.1` (custom Vulkan build) → `gpustack/gpustack:v2.1.2` (upstream) | `llm` (EXPERIMENTAL — opt-in) | Apache-2.0 |

**What's new in 2026.05**

- Single `llm` profile + `HARDWARE=amd|nvidia|cpu` selector replaces the three legacy profiles (auto-migrated by `razzfazz-upgrade.sh` Step 5b).
- Three named custom backends registered idempotently by `llm/gpustack/init-backends.py` (`llama-box-vulkan-custom`, `llama-box-rocm-custom`, `llama-box-cpu-custom`); registration now runs *before* model deploy so the first-deploy never stalls on "no backend versions available for GPU device".
- AMD GPU device access fixed for fresh installs and volume resets: gpustack containers now run with the host's `render` group as a numeric supplementary gid (`/dev/dri/renderD128` + `/dev/kfd` were unreadable pre-fix, every model deploy hung at `ACCEL_WORKING -13 EACCES`).
- Worker ports `10150-10151` are now mode-aware: `127.0.0.1` in standalone mode, `0.0.0.0` in master mode (was unconditionally `0.0.0.0`); both `init.sh` and a new `migrate_gpustack_bind` step in `upgrade.sh` write the right values.
- New `autoheal` sidecar restarts containers in Docker `unhealthy` state; healthchecks added to every gpustack service.
- Kernel sysctl tunables (`vm.panic_on_oom=0`, `kernel.panic=10`) replace the earlier silent host hang with the OOM killer reaping the largest process; the prior `=1` panic-on-oom default proved too aggressive on tight-memory boxes.
- `--system-reserved` soft-limit (defaults `8 GiB RAM` + `4 GiB VRAM`) prevents the scheduler from over-packing.
- Management UI → Settings → GPUStack now has a 📋 button to copy the API key to clipboard, and a banner cross-link to the new LLM Runtime panel.
- Repositioned to EXPERIMENTAL late in the cycle after stop/start memory leak observed on production AMD hardware (slow growth across cycles, separate from upstream PR #5255 already mitigated by the bus.py / cache.py backport). v0.7.1 stays as STABLE default; this profile is opt-in only.

**Security**

- F-RC5-1 fully closed: the rc5-era patch only covered `GPUSTACK_HOST_BIND` (port 9090); rc6.9 closes the symmetric leak on `GPUSTACK_WORKER_HOST_BIND` (ports 10150-10151) by writing both per-`GPUSTACK_MODE` from init.sh and converging existing boxes from upgrade.sh.
- Bundled CUDA/ROCm wheels in the v2.x unified image carry a sizable inherited CVE count; no upstream patch path. Documented as accept-residual; mitigated by loopback bind, Authentik forward_auth gating, and the local-code-execution prerequisite for exploitation. The cycle's runtime repositioning (v0.7.1 as default) reduces production exposure: only operators who specifically opt into v2.1.x carry these CVEs.

**Migration**

- Models deployed pre-rc6.7 with the obsolete bare `backend='llama-box'` field self-heal on the next `init-backends.py` run (resolves to the per-`HARDWARE` replacement).
- Switching from EXPERIMENTAL to STABLE via the LLM Runtime panel resets `gpustack_db` (alembic schemas are incompatible across v0.7.1 ↔ v2.x) and re-mints `GPUSTACK_API_KEY`; loaded model definitions do not auto-migrate.

---

#### <img src="/branding/media/razzfazz-ai_llm_icon.png" width="20" height="20"> GPUStack (STABLE — v0.7.1 + Vulkan, AMD)

| Version | Profile | License |
|---|---|---|
| `0.7.1` (custom Vulkan build, unchanged) | `llm-legacy` (STABLE — default for AMD) | Apache-2.0 |

**What's new in 2026.05**

- Repositioned mid-cycle from "rollback safety net" to **STABLE default for AMD**. The image is unchanged from the prior cycle; what changed is the customer-facing posture after a 12-hour stop/start soak validated this path leak-free under sustained dual-model load on production AMD Strix Halo hardware (2460 iterations / 99.92% HTTP 200 / RSS started 218 MB and ended 167 MB).
- Container_name `gpustack` is shared with the v2.x service (mutually exclusive). The Configuration Portal LLM Runtime toggle handles the swap atomically.
- ollama-proxy now reads the canonical `GPUSTACK_API_KEY` env (was the legacy `GPUSTACK_OPENAI_API_KEY` which got dropped from `.env.example` in rc6.7 #61/#62 — rc6.9 finishes the rename so fresh installs without the legacy var no longer hit "OPENAI_API_KEY cannot be empty").
- Patch line `ga.1`: a custom-built llama.cpp Vulkan runner (b9112 + `ggml-org/llama.cpp#22458`) at `llm/gpustack/patched-llama-cpp/` replaces the stock runner for gemma4 vision on AMD Strix Halo. The stock build crashes on the second image in a session with a `tensor->data NULL` assertion in the SWA prompt-cache path. The build script is reproducible; binaries are not committed.

**Security**

- Same `GPUSTACK_*_HOST_BIND` mode-aware policy as the EXPERIMENTAL profile (rc6.9 F-RC5-1 closure applies symmetrically to both runtime versions).

---

#### <img src="/branding/media/razzfazz-ai_llm_icon.png" width="20" height="20"> GPUStack (STABLE — v0.7.1-cpu, CPU)

| Version | Profile | License |
|---|---|---|
| `gpustack/gpustack:v0.7.1-cpu` (unchanged) | `llm-cpu` (STABLE — default for CPU) | Apache-2.0 |

**What's new in 2026.05**

- Un-deprecated mid-cycle. The earlier "DEPRECATED, retained one cycle for in-place upgrade compatibility" notice is reversed by the runtime repositioning: `llm-cpu` is now the **STABLE default for CPU installs**, not a transitional path.
- The `master-cpu` and `testvm-cpu` package presets land here by default; new installs no longer need `--llm-experimental` to avoid the v2.1.x-on-CPU bundled-deps CVE surface.
- Earlier rc5.1 attempted slim-CPU mitigation via a `:v2.x-cpu` image variant was abandoned in rc6.5 after the variant proved to never have shipped on Docker Hub; the unified image continues to serve master/worker/NVIDIA via the v2.x `llm` profile, while CPU installs default to this leaner v0.7.1-cpu path.

**Migration**

- `razzfazz-upgrade.sh` Step 5b auto-migrates `COMPOSE_PROFILES=llm-cpu` → `llm` and the matching `HARDWARE` + `COMPOSE_FILE` env entries.

---

### Knowledge & RAG

#### <img src="/branding/media/razzfazz-ai_cognee_icon.png" width="20" height="20"> Cognee

| Version | Profile | License |
|---|---|---|
| `1.0.1 → 1.1.0` with embedded Kuzu + Next.js frontend | `cognee` | Apache-2.0 |

Sub-images bumped together with the main image:

| Sub-image | Version |
|---|---|
| razzfazz-cognee-frontend | NEW — built locally from the upstream `cognee-frontend/` subtree (mirrors the paperclip pattern) |

**What's new in 2026.05**

- Embedded Kuzu graph replaces the FalkorDB sidecar — one less container, no extra adapter, simpler backup.
- The bare FastAPI Swagger landing page is replaced by the upstream Next.js frontend (Caddy splits routing on the cognee subdomain so `/api/*`, `/docs*`, `/openapi.json`, `/redoc`, `/health*` go to the FastAPI backend and everything else hits the new frontend container).
- Cognify works on a fresh container without manual onboarding (proxy-headers + onboarding bypass landed mid-cycle).
- Container host port moved from `8000` to `8011` to free `:8000` for the OpenHands per-conversation sandbox runtime, which hardcodes that port in docker-local mode.

**Migration**

- `FALKORDB_PORT` and `FALKORDB_VERSION` are removed from `.env` automatically.
- `FALKORDB_PASSWORD` (if set) can be deleted manually — nothing reads it any more.
- `COGNEE_PORT` 8000 → 8011 migrated automatically (existing operators with the manual workaround applied keep their override).
- `COGNEE_FRONTEND_PORT` (default 8012) added to `.env` for the new Next.js frontend host port binding.
- Embedding endpoint and provider corrected for GPUStack (`openai_compatible` + `/v1`).

---

#### <img src="/branding/media/razzfazz-ai_rag_icon.png" width="20" height="20"> LightRAG

| Version | Profile | License |
|---|---|---|
| `→ v1.4.16` (`v` prefix added in upstream tags) | `lightrag` | MIT |

**What's new in 2026.05**

- Runs unchanged; tag-format change tracked in `manifests/versions.json`. `ga.4` aligned the compose-default tag (`v1.4.15` → `v1.4.16`) with `.env.example` after a drift audit.

---

#### Onyx

| Version | Profile | License |
|---|---|---|
| `→ v3.2.12` | `onyx` | MIT |

**What's new in 2026.05**

- Lockstep bump across all 6 containers (api, background, web, model-server, model-indexer, vespa).

---

#### Apache Tika

| Version | Profile | License |
|---|---|---|
| `→ 3.3.0.0-full` | `tika` | Apache-2.0 |

**What's new in 2026.05**

- Carried forward; document text + metadata extraction REST API unchanged.

---

### Document Processing

#### <img src="/branding/media/razzfazz-ai_docling_icon.png" width="20" height="20"> Docling-serve

| Version | Profile | License |
|---|---|---|
| `→ v1.18.0` | `docling` | MIT |

**What's new in 2026.05**

- VLM path uses GPUStack's `granite-docling` model deployment.

**Security**

- Trivy scan: 0 Critical / 3 High → **0 Critical / 2 High**; closes the BentoML CVE-2024-12760 open-redirect from v1.16.1.

---

#### Presidio

| Version | Profile | License |
|---|---|---|
| Analyzer/Anonymizer `→ 2.2.362`, Image-Redactor `→ 0.0.58` | `presidio` | MIT |

**What's new in 2026.05**

- PII detection (Analyzer) and image redaction (Image Redactor) REST APIs unchanged.

---

#### <img src="/branding/media/razzfazz-ai_pdf_icon.png" width="20" height="20"> Stirling-PDF

| Version | Profile | License |
|---|---|---|
| `→ 2.10.1` | `stirling-pdf` | MIT |

**What's new in 2026.05**

- Web UI carries forward 50+ PDF operations (merge, split, OCR, compress, convert) under Authentik SSO.

---

#### Gotenberg

| Version | Profile | License |
|---|---|---|
| `→ 8.32.0` | `gotenberg` | MIT |

**What's new in 2026.05**

- Patch line `ga.1`: bumped 8.31.0 → 8.32.0 because 8.31.0 crash-loops on the `--chromium-deny-private-ips` flag we pass (became `--chromium-deny-private-ips=true`-only in 8.32). 8.32 also reverts the strict private-IP default, so we re-enable the flag explicitly.

---

#### <img src="/branding/media/razzfazz-ai_documents_icon.png" width="20" height="20"> paperless-ngx

| Version | Profile | License |
|---|---|---|
| `→ 2.20.15` | `paperless-ngx` | GPL-3.0 |

**Security**

- GHSA-8c6x-pfjq-9gr7 closed by the upstream bump.

---

### Search & Crawl

#### <img src="/branding/media/razzfazz-ai_search_icon.png" width="20" height="20"> SearXNG

| Version | Profile | License |
|---|---|---|
| `→ 2026.5.17-f26e45077` (CalVer + git-sha) | `searxng` | AGPL-3.0 |

**What's new in 2026.05**

- Marked `internal_only: true` in `profiles.yaml` — consumed by OpenWebUI / Dify via Docker DNS, not via Caddy.
- Patch line `ga.4`: rolled forward across the cycle from 2026.4.29 (GA) → 2026.5.9 → 2026.5.17-f26e45077 (immutable CalVer + git-sha pin).

**Security**

- Trivy scan: 0 Critical / 1 High → **0 Critical / 0 High**.

---

#### <img src="/branding/media/razzfazz-ai_search_icon.png" width="20" height="20"> Crawl4AI (NEW module)

| Version | Profile | License |
|---|---|---|
| `0.8.6` (introduced) | `crawl4ai` | Apache-2.0 |

**What's new in 2026.05**

- New module: RAG-friendly web crawler with Markdown extraction, structured scraping, and browser automation.
- Pairs with SearXNG: SearXNG for URL discovery, Crawl4AI for content fetch + Markdown distillation into LightRAG / Cognee / Onyx / Dify knowledge bases.
- Surfaces REST API at `/crawl`, `/scrape`, `/llm`; OpenAPI at `/docs`; operator playground at `/playground`; monitoring at `/dashboard`.

**Security**

- `SYS_ADMIN` cap is justified by the Chromium sandbox (alternative `--no-sandbox` is worse for a crawler executing arbitrary HTML/JS) and scoped under `no-new-privileges:true`.

---

### Collaboration & Vault

#### <img src="/branding/media/razzfazz-ai_matrix_icon.png" width="20" height="20"> Synapse / Element Web

| Version | Profile | License |
|---|---|---|
| Synapse `→ v1.152.1`; Element Web `→ v1.12.18` | `matrix` | AGPL-3.0 |

**Security**

- Synapse v1.152.1 fixes the admin-bypass regression + DoS / pagination GHSAs flagged by upstream.
- Patch line `ga.4`: Element Web v1.12.17 → v1.12.18.

---

#### <img src="/branding/media/razzfazz-ai_vault_icon.png" width="20" height="20"> Vaultwarden

| Version | Profile | License |
|---|---|---|
| `→ 1.36.0` | `vaultwarden` | GPL-3.0 |

**What's new in 2026.05**

- Self-hosted Bitwarden-compatible password manager carries forward with Authentik OIDC SSO; cycle picks up 4 named SSO/SSRF upstream fixes on the bump.

---

#### <img src="/branding/media/razzfazz-ai_secrets_icon.png" width="20" height="20"> Infisical

| Version | Profile | License |
|---|---|---|
| `v0.146.0-postgres → v0.159.28` | `infisical` | MIT |

**What's new in 2026.05**

- Image-variant change: upstream `-postgres` variant was discontinued (last `-postgres` tag 2025-08-08); we move to the default image. Drop-in compatible — same `DB_CONNECTION_URI` env.
- Patch line `ga.4`: no infisical bump applied — v0.159.29 reported by Phase 2 audit subagent does not exist on Docker Hub; v0.159.28 IS the latest stable.

**Migration**

- 13 minors of upstream changes between 0.146 → 0.159; review the Infisical upstream changelogs if you depend on specific Infisical features.

---

### Personal Agents

These all run per-Authentik-user since 2026.05; provisioning is via the **My Agents** Authentik drawer. The legacy global profiles (`hermes`, `moltis`, `coding-tools`) are removed.

---

#### <img src="/branding/media/razzfazz-ai_hermes_icon.png" width="20" height="20"> Hermes Agent

| Version | Profile | License |
|---|---|---|
| `:latest` (mutable) → **`v2026.5.16`** (immutable, built from upstream source) | `agents` (per-user) | Apache-2.0 |

**What's new in 2026.05**

- Mutable `:latest` tag replaced with an immutable release tag; future bumps are explicit. Cycle moved `v2026.4.23` (rc6) → `v2026.5.7` ("Tenacity Release", late-GA) → `v2026.5.16` (`ga.4`).
- Container is now built locally from the upstream Hermes source repo (rc6.7) instead of pulling a pre-built image; pinning the upstream commit makes provenance auditable.
- A documentation page on Hermes CLI access from the container plus the `hermes setup` wizard ships in the Help Center to cover the dashboard ↔ workspace auth gap.
- Idle auto-stop is disabled by default for per-user agents (operators can re-enable per user from the Configuration Portal).
- Hermes-Workspace companion image moved `v2.1.3` → `v2.3.0` in `ga.4`.

**Security**

- `hermes-agent` Debian-base CVE counts remain elevated (improved by `node:20-bookworm-slim` → `node:22-trixie-slim`, but Node-ecosystem CVE counts on any current Debian base are inherently high).

---

#### <img src="/branding/media/razzfazz-ai_moltis_icon.png" width="20" height="20"> Moltis

| Version | Profile | License |
|---|---|---|
| `:latest` (mutable) → **`20260517.01`** (immutable, built from upstream source) | `agents` (per-user) | MIT |

**What's new in 2026.05**

- Mutable `:latest` tag replaced with an immutable release tag. Cycle moved `20260429.01` (rc6) → `20260508.01` (GA-window) → `20260510.01` (`ga.3`-era) → `20260517.01` (`ga.4`).
- Container is now built locally from the upstream Moltis source repo (rc6.7); same provenance benefit as Hermes.
- The Moltis model picker in the Configuration Portal now shows the live GPUStack model list (was a static placeholder pre-fix).

---

#### <img src="/branding/media/razzfazz-ai_coding_icon.png" width="20" height="20"> Coding Tools (per-user)

| Version | Profile | License |
|---|---|---|
| `gsd-pi 2.82.0` + `opencode-ai 1.15.3` (Dockerfile ARG-pinned, full-cycle) | `agents` (per-user) | MIT |

**What's new in 2026.05**

- npm package versions pinned via Dockerfile ARG; was rolling on `:latest`. Cycle moved gsd-pi `2.78.1` (rc6) → `2.80.0` (`ga.1`) → `2.82.0` (`ga.3`); opencode-ai `1.14.29` (rc6) → `1.14.42` (GA) → `1.14.46` (`ga.3`) → `1.15.3` (`ga.4`).

---

#### <img src="/branding/media/razzfazz-ai_openhands_icon.png" width="20" height="20"> OpenHands

| Version | Profile | License |
|---|---|---|
| `→ 1.7.0` | `agents` (per-user) | MIT |

**What's new in 2026.05**

- Same image now serves the per-user provisioning path; runtime image moves in lockstep with the main image.
- Per-conversation sandbox runtime now path-proxies through the parent OpenHands container so `cognee:8000` no longer collides with the sandbox's hardcoded port (Cognee moved to `:8011` to make room).
- Sandbox container URL pattern env corrected to the upstream-canonical `SANDBOX_CONTAINER_URL_PATTERN` (was `OH_SANDBOX_*`).
- Hermes / OpenHands now target the OpenAI-compatible `/v1-openai` endpoint (was the GPUStack-native `/v1`).
- Patch line `ga.4`: bumped 1.6.0 → 1.7.0 across the global compose path, the sandbox runtime image (`ghcr.io/openhands/runtime:1.6.0-nikolaik` — runtime versioning is decoupled from main openhands), and the per-user catalog entry.

---

#### <img src="/branding/media/razzfazz-ai_paperclip_icon.png" width="20" height="20"> Paperclip

| Version | Profile | License |
|---|---|---|
| `v2026.403.0 → v2026.513.0` | `agents` (per-user) | MIT |

**What's new in 2026.05**

- Custom-built; rebuilds automatically during `razzfazz-upgrade.sh`. Cycle moved `v2026.403.0` → `v2026.428.0` (GA) → `v2026.513.0` (`ga.4`).
- Auto-onboard on first boot: new instances no longer require manually walking through the create-account flow before they're usable.
- Container data dir is now correctly chowned at provisioning so the container can write its state on first run.
- Invitation URL is surfaced in the container's logs after onboarding, so operators can hand it to invited users without scraping the UI.
- gsd-pi 2.78.1 → 2.80.0 + opencode npm dependency pin (subsequent gsd-pi / opencode bumps tracked in the Coding Tools section).

---

### Observability

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> OpenLIT + ClickHouse (NEW profile)

| Version | Profile | License |
|---|---|---|
| OpenLIT `1.20.0` (introduced); ClickHouse `24.8.4.13` (introduced) | `observability` (opt-in) | Apache-2.0 / Apache-2.0 |

**What's new in 2026.05**

- New profile delivers stack-wide LLM observability — OpenLIT OTLP filter pipeline ingests OpenTelemetry traces; normalised and forwarded into ClickHouse. (OpenLIT moved 1.18.1 → 1.20.0 during the cycle.)
- ClickHouse first-boot schema initialiser takes ~100s; healthcheck `start_period` accommodates this.
- Pre-backup cleanup hook strips conversation maps so chat-history doesn't leak into long-term snapshots.
- Patch line `ga.4`: ClickHouse `mem_limit` raised 2 GiB → 6 GiB + `merge_tree.parts_to_throw_insert=300` / `parts_to_delay_insert=150` backpressure. Discovered during prod investigation 2026-05-17: ClickHouse was OOMing under its own `system.text_log` / `system.asynchronous_metric_log` merge backlog (engine self-logging churn, not application telemetry). See *Known issues* below — the OOM symptom is fixed but the writer-side pipeline that should feed real data is non-functional and tracked as M033-S11.

---

### Operations

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Komodo

| Version | Profile | License |
|---|---|---|
| `→ 2.2.0` (lockstep with `komodo-periphery`) | `monitor` | GPL-3.0 |

**What's new in 2026.05**

- Carried forward; image bumps inside the cycle, no API changes.

---

#### <img src="/branding/media/razzfazz-ai_git_icon.png" width="20" height="20"> Gitea

| Version | Profile | License |
|---|---|---|
| `→ 1.26.1` | `gitea` | MIT |

**What's new in 2026.05**

- Carried forward at the latest stable upstream version.
- Public signup hardcoded off (security-default policy from earlier audit).

---

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Start Portal (NEW module)

| Version | Profile | License |
|---|---|---|
| Custom-built Flask app | `start-portal` (always-on) | Apache-2.0 |

**What's new in 2026.05**

- Brand-new tile-based landing page at `start.<your-domain>` showing every enabled module with status, RAM usage, country-of-origin flag, and click-through to the underlying app.
- Per-user pinning via a heart-icon toggle sticks favourite tiles to the top of the user's view.
- Drag-and-drop reorder works across categories; tile glyph falls back to a custom SVG when an `app_icon` is absent.
- Operators rename, reorder, add, and delete custom categories per user (a small SQLite database in the container holds preferences).
- Multi-column responsive layout adapts to viewport.
- Authentik forward_auth gates access; My Agents drawer cross-link sits next to the Logout button.

**Migration**

- `START_DOMAIN` (default `start.${MAIN_DOMAIN}`) was added to `.env.example` mid-cycle and registered in `migrations/env-changes.json` so 2026.04-ga upgrades pick it up automatically; pre-fix the missing var caused a Caddy crash loop on the next `docker compose up -d`.

---

#### Backup pipeline

| Version | Profile | License |
|---|---|---|
| `offen/docker-volume-backup` carried forward | core (always-on) | MIT |

**What's new in 2026.05**

- Backup management UI and dashboard backup card both list `.tar.gz.gpg` (encrypted) backups in addition to legacy `.tar.gz` (encryption is on by default since the prior cycle, when `BACKUP_ENCRYPTION_PASSWORD` started auto-generating in `razzfazz-init.sh`).
- Encrypted backups are now restorable: the management container's `restore_full` and `restore_partial` decrypt with `gpg --batch` before `tarfile.open`, then clean up the temp file.
- New env-snapshots panel surfaces `.env` snapshots taken automatically by `migrate_env` at every upgrade — useful for cross-referencing what `.env` looked like before each migration.
- CLI `./razzfazz-backup.sh status` reports counts per category, including `Encrypted: N (.tar.gz.gpg form)`.

---

#### Autoheal sidecar (NEW)

| Version | Profile | License |
|---|---|---|
| `willfarrell/autoheal:1.2.0` | core (always-on) | MIT |

**What's new in 2026.05**

- New sidecar watches every container with the `autoheal=true` label and restarts any in Docker `unhealthy` state.

**Security**

- The image is on Alpine 3.13.5 with sizable CVE debt; upstream is in maintenance mode. Compensating controls: no inbound network exposure; only this container retains rw access to raw `/var/run/docker.sock` (everything else routes via `docker-socket-proxy`). Replacement queued for the next cycle (custom-built ~50-LoC alpine + docker-cli + bash).

---

#### docker-socket-proxy

| Version | Profile | License |
|---|---|---|
| Tecnativa allowlist proxy | core (always-on) | Apache-2.0 |

**What's new in 2026.05**

- Keeps 5 containers off the raw Docker socket; only `autoheal` retains RW access.

---

## Operator UX — Management UI improvements

This cycle landed a substantial Management UI polish pass during release-candidate testing:

- **Dashboard health** card no longer false-`degraded` when an operator trims `COMPOSE_PROFILES` — Exited containers from inactive profiles are now correctly excluded from the running/total ratio.
- **Modules overview** maturity filter is data-driven from `profiles.yaml` (was hardcoded; previously missed `deprecated` and falsely showed `stable`).
- **Help center** shows all 24 documentation tiles to anonymous visitors; gated apps are no longer hidden from operators without an Authentik session (the apps themselves remain Authentik-gated).
- **Help cache auto-warms** on container start so new modules added to `mirror_config.json` populate without an admin click.
- **Release notes / What's New** popups prefer the consolidated cycle-level doc over per-rc historical notes.
- **License overview** lists all components grouped into 12 sections, derived from a per-section template loop (was hardcoded — drifted every cycle).
- **GPUStack tab** has a 📋 button to copy the configured API key to clipboard.
- **LLM Runtime panel** under Modules → LLM Runtime flips between STABLE (v0.7.1) and EXPERIMENTAL (v2.1.x) in one click; cross-link banners on All Modules, the three LLM module detail pages, and Settings → GPUStack route operators to the toggle from anywhere they might want it.
- **Backup section** lists encrypted backups + env-snapshots in two panels (both were silently missing before).
- **Post-upgrade reminder** at the end of every successful upgrade points operators at `./razzfazz-post-install.sh --refresh` (idempotent: refreshes `/etc/hosts`, provisions `GPUSTACK_API_KEY`, re-registers v2.x custom backends when the active runtime is `llm`, warms the help cache; doesn't touch models/plugins/operator state).
- **`--preset`** on a previously-initialised stack now refuses to run without `--force` and surfaces exactly what would be overwritten.
- **Apply / streaming progress** for the LLM Runtime toggle now shows real-time per-step output (was silent post-rc6.7 due to a wire-shape mismatch between server SSE envelope and client renderer).

---

## Upgrade instructions

### From 2026.04-ga → 2026.05-ga.4 (the supported customer path)

Big-bang upgrade via the **bootstrap script**. The bootstrap is required because 2026.04-ga's own `razzfazz-upgrade.sh` predates the in-place re-exec mechanism (added later in the 2026.05 cycle); without it, bash keeps executing the OLD script from memory after `git checkout` and the new migration logic never runs.

```bash
cd ~/razzfazz-ai-service-stack

# 1) Fetch the bootstrap (only on 2026.04-ga; later versions have it in-tree)
git fetch --tags origin
git show v2026.05-ga.4:razzfazz-upgrade-from-2026.04-GA.x.sh > razzfazz-upgrade-from-2026.04-GA.x.sh
chmod +x razzfazz-upgrade-from-2026.04-GA.x.sh

# 2) Run the upgrade
./razzfazz-upgrade-from-2026.04-GA.x.sh --target v2026.05-ga.4

# 3) After the upgrade reminder block prints, run the post-upgrade refresh
./razzfazz-post-install.sh --refresh
```

The bootstrap fetches the target tag, swaps `razzfazz-upgrade.sh` on disk, validates with `bash -n`, removes itself from the working tree, and `exec`s the new script. Bash loads it fresh from line 1 so every new function (LLM-profile migration, kernel sysctl tunables, host hardening, the cycle's `migrate_env` parser fixes) is live.

### Tester one-pager (testvm-cpu preset, 2026.04-ga.1 → 2026.05-ga.4)

For SEQIS testers running the stack in `testvm-cpu` mode (Docker Compose, CPU-only LLMs, self-signed TLS) on a tester VM, a step-by-step one-pager is shipped in the repo:

> [`docs/upgrade-guide-testvm-cpu-2026.04-ga.1-to-2026.05-ga.4.md`](https://git.razzfazz.ai/razzfazz.ai/razzfazz-ai-service-stack/src/tag/v2026.05-ga.4/docs/upgrade-guide-testvm-cpu-2026.04-ga.1-to-2026.05-ga.4.md)

The one-pager is the recommended path for tester VMs (the production-side bootstrap above applies but the one-pager is friendlier and adds testvm-specific verification + the known-issues + rollback flow that testers actually need). Highlights it covers that aren't in the generic bootstrap section above:

- **Two-command path:** the two commands a tester runs (no flags to remember). ~15–30 min unattended.
- **Pre-flight checklist:** disk-space requirement (≥10 GB free on `/var/lib/docker`), required egress hosts (`git.razzfazz.ai`, `ghcr.io`, `docker.io`, `marketplace.dify.ai`), in-flight-work quiesce reminder.
- **Verification commands** for `cat VERSION` / `docker compose ps` / `docker inspect authentik-server` to confirm `2026.2.3` post-upgrade.
- **What's new in ga.4 (tester-facing slice):** Authentik 2.3 SSO security upgrade, PostgreSQL CVE coverage, OpenHands → 1.7.0, per-user agent image refresh, ClickHouse observability OOM hotfix, plus the known OpenLIT pipeline non-functional issue (tracked for the next cycle).
- **Targeted troubleshooting:** the three real failure modes seen on tester VMs (`Containers in restart loop`, `caddy` `rate_limit not a registered directive`, `Host hardening failed`) with the one-command remediation for each.
- **Rollback section** — `./razzfazz-upgrade.sh --rollback` semantics and fallback if rollback itself fails.
- **`--with-acceptance` opt-in** for running the bundled acceptance probe suite immediately after the upgrade body finishes (recommended on tester VMs).
- **Alternate path** for testers already on a 2026.05-rc / earlier 2026.05-ga.N (no bootstrap needed — in-tree `razzfazz-upgrade.sh` suffices).

If you're upgrading multiple tester VMs, point each one at the same one-pager URL above — it's pinned to `v2026.05-ga.4` so it stays valid even after the tag advances.

### AMD Strix Halo host prep

For AMD Strix Halo boxes you also need to upgrade the kernel + ROCm together (to support the new gpustack v2.1.x runtime). The stack `razzfazz-upgrade.sh` does NOT perform host-level changes — those are deliberate operator actions. There's an automation script:

```bash
sudo ./razzfazz-host-upgrade.sh                # full run — TWO reboots
sudo ./razzfazz-host-upgrade.sh --dry-run      # show what would happen
```

What it does:

| Stage | Action | Reboot |
|---|---|---|
| **A** (pre-reboot 1) | ROCm 6.4 → 7.2 via `amdgpu-install --no-dkms` | reboot 1 → boot 6.14, swap live DKMS module → in-tree `amdgpu.ko` |
| **B** (post-reboot 1) | Sanity-gate ROCm 7.2 in-tree, unhold 6.14, install `linux-oem-24.04d`, `GRUB_DEFAULT=0`, lift `linux-image-*` blacklist from unattended-upgrades | reboot 2 → boot the OEM kernel |
| **C** (post-reboot 2) | Sanity-gate kernel + ROCm together, clean up, disable resume unit | none |

Resume across reboots is a one-shot systemd unit installed in stage A and disabled in stage C. State file at `/var/tmp/.razzfazz-host-upgrade-stage` survives reboots. Run from a persistent SSH session (or `screen` / `tmux`) — total runtime ~20–30 min including downloads.

**Rollback:** GRUB still has the `6.14.0-37-generic` submenu entry. Boot it from the menu, `apt-mark hold` the 6.14 packages, re-install ROCm 6.4 DKMS via the deb in `llm/amd/`.

### Incremental upgrades within the cycle

Boxes already on a 2026.05-rcN or earlier 2026.05-ga.N have the in-place re-exec mechanism. `./razzfazz-upgrade.sh --target v2026.05-ga.4` is the right command — bootstrap is unnecessary but harmless.

### Offline / air-gapped

`./razzfazz-package.sh v2026.05-ga.4` builds the offline package on a developer machine (use `--include-images` for fully air-gapped targets — large). The package bundles the new script, so `./razzfazz-upgrade.sh --package <file>` runs the new code directly.

### Rollback

```bash
./razzfazz-upgrade.sh --rollback                       # script + .env
./razzfazz-backup.sh restore <pre-upgrade-backup>      # full data
```

For LLM-profile-only rollback (back to the frozen 0.7.1 + Vulkan stack) without rolling back the whole release:

```bash
# In .env: replace `llm` with `llm-legacy` in COMPOSE_PROFILES
docker compose up -d --force-recreate --remove-orphans gpustack model-sync
```

---

## Security

Two audits this cycle: a baseline at the start, and a post-host-upgrade pass that surfaced 7 new findings; 5 closed by GA. A third pre-GA pass on `2026-05-08` adds the late-cycle F-RC5-1 closure and confirms the runtime stability repositioning *reduces* production CVE exposure (bundled-deps CVEs only carry on EXPERIMENTAL opt-in installs).

### Cycle CVE deltas

| Image | 2026.04-ga | 2026.05 | Driver |
|---|---|---|---|
| `dify-api` | 7C / 90H | **4C / 75H** | LiteLLM 1.82.x → 1.83.0; CVE-2026-35030 closed |
| `docling-serve-cpu` | 0C / 3H | **0C / 2H** | BentoML open-redirect fix |
| `searxng` | 0C / 1H | **0C / 0H** | CalVer roll-forward |
| `dify-worker` LiteLLM | 1.82.7 / 1.82.8 supply-chain risk | **1.83.0** | Out of supply-chain malware window |
| pgvector across all DBs | 0.8.x | **0.8.2** | Above CVE-2026-3172 cutoff |

Nuclei (outside + inside × 2): **0 matches**.

### Audit findings closed this cycle

- **gpustack admin UI LAN-exposed** — closed via `GPUSTACK_BIND` / `GPUSTACK_HOST_BIND` split + ufw rules.
- **Patch bumps don't propagate via migrate_env** — closed via 3 `change_default` rules + `prepare-release.sh` validator.
- **Raw socket on rc5 services** — closed (re-classified misdiagnosis: services use `:ro`, only `autoheal=true` containers get RW; documented architectural choice).
- **Orphan ad-hoc container** — closed.
- **Sysctl tunables not installed on upgrade** — closed via fatal-on-fail + sudo-prime in `razzfazz-upgrade.sh`; `--skip-host-updates` to bypass.

### Open / accept-residual

- **gpustack v2.1.2 bundled-deps CVE inheritance (35C / 152H).** No upstream patch path. Mitigated by loopback bind + Authentik forward_auth gating + bundled-dep nature of the CVEs (require local code-execution context). Will bump on first upstream patch.
- **autoheal 1.2.0 / Alpine 3.13.5 base CVE debt (8C / 37H).** Upstream is in maintenance mode. Compensating controls: no inbound network exposure; `docker-socket-proxy` allowlist for other consumers. Replacement queued for the next cycle.
- **Open-WebUI 0.9.1 CVE backlog.** No upstream fix path; tracked on the patch watchlist.
- **`hermes-agent` Debian base CVE counts.** Improved by base bump (`node:20-bookworm-slim` → `node:22-trixie-slim`); Node-ecosystem CVE counts on any current Debian base are inherently elevated.
- **Shared admin password.** Printed on box sticker; rotated at customer takeover.

---

## Known issues at GA

- **gpustack v2.1.2 bundled-deps CVE inheritance** + **autoheal Alpine base CVE-debt** — see Security section above.
- **CPU runner** (`llama-box-cpu-custom`) ships, validated on the testvm-cpu box only; AMD/ROCm paths got the most exhaustive endurance testing.
- **NVIDIA hardware overlay** designed but UNTESTED in the SEQIS fleet — only AMD Strix Halo (gfx1151) and CPU were validated end-to-end. NVIDIA customers should validate before relying.
- **MLX (Apple Silicon) workers** are not yet integrated. Architecture sketched in design docs; would land in a future cycle.
- **Host-level OOM mitigated, not eliminated.** The stability set (`--system-reserved` + autoheal + sysctl panic-on-oom + reboot) prevents the silent-hang failure from earlier stress testing, but a single chronically-busy runner can still grow its host-side state past the reservation in extreme cases. Behaviour is now: kernel reboots in 10 s, stack returns automatically. Operator-visible signal: a noticeable downtime window (~60–90 s).
- **`razzfazz-upgrade.sh` pre-upgrade backup encryption gap** (new in `ga.2` known-issues list). The `backup-service` container reads `GPG_PASSPHRASE` at container start, not at command invocation; on a box where the env was empty when the container last started, every subsequent `docker exec backup-service backup` produces an unencrypted `.tar.gz`. Mitigated by force-recreating `backup-service` before any release with `docker compose up -d --force-recreate backup-service`. Tracked for the next maintenance cycle.
- **Manifest `change_default` migrations skip on `installed == target`** (new in `ga.2` known-issues list). Re-upgrading a box that's already on the target version silently bypasses `change_default` rules, so version drift can persist past a re-upgrade. Workaround for fleet alignment is to bump to a newer patch tag. Tracked for the next maintenance cycle.

---

## Patch releases

### `v2026.05-ga.1` (released 2026-05-13)

Highlights folded into the per-module sections above. The headline themes:

- **Caddy network-alias refactor** retires `CADDY_IP` / `extra_hosts` — see the new cross-cutting section above.
- **LLM single source of truth** — `core/llm/standard-models.yaml` + `core/llm/sync.py` + the `propagate-llm-config` operator skill.
- **Agent-provisioning hardening** — seven follow-up source fixes that bake in lessons from the overnight session of 2026-05-12/13: upgrade self-heal for partial-failure container state, agent-manager `catalog.py` bind-mount fix, `docker exec -u uid` correctness, OpenHands subdomain fix, paperclip auto-allowlist, Caddy stale-ACME-lock cleanup, Caddy broken-cert self-heal.
- **Patched llama.cpp Vulkan build** for the gemma4 SWA prompt-cache crash on AMD Strix Halo — see the GPUStack STABLE module section above.
- **OpenWebUI** `0.9.4 → 0.9.5` patch bump (sec/bug fixes per upstream).
- **gotenberg** `8.31.0 → 8.32.0` — 8.31.0 crash-loops because the `--chromium-deny-private-ips` flag we pass became `--chromium-deny-private-ips=true`-only in 8.32.

### `v2026.05-ga.2` (released 2026-05-13)

A focused Dify password-reset usability patch — see the new cross-cutting section above for the upstream-regression analysis and the per-module Dify section for the migrated env keys and the entrypoint wrapper.

### `v2026.05-ga.3` (released 2026-05-13)

The cycle-closing patch. Headline themes:

- **M031 S1-S4 — single-YAML LLM config completes.** `catalog.py` now reads `core/llm/standard-models.yaml` via the new `llm_config` helper. `coding-tools` entrypoint, gsd `models.json`, opencode config, and Paperclip's `OPENCODE_GPUSTACK_CONFIG` all generate from the same YAML. `razzfazz-post-install.sh` reads model definitions from YAML and invokes `core/llm/sync.py` at the end to reconcile every gpustack-consuming service. `sync.py` extended with `sync_lightrag` / `sync_paperclip` / `sync_openhands`. Closes the qwen3.5 / qwen3.6 drift surfaced during fleet validation.
- **M030 S4 — per-user agent state included in `razzfazz-backup.sh`.** Hermes / Moltis / Coding-Tools / OpenHands / Paperclip user state is now snapshotted by default. `--skip-agents` opt-out for size-constrained snapshots.
- **Security review (Mode A) executed against the full stack.** `security-run/razzfazz-ai-box-security-assessment-v2026.05-ga.3.md` is the audit artifact. Documentation refresh: `docs/security-architecture.md` rewritten across 10 sections; customer handover doc bugs fixed (security review no longer punted to the customer, bootstrap-password rotation properly documented).
- **Pre-tag tripwire + strengthened pre-flight gates.** `scripts/pre-tag-check.sh` now blocks any `v*-ga*` tag whose diff touches the auth-relevant surface without a fresh `security-run/*-<tag>.md` artifact. `.git/hooks/pre-push` enforces this even when `prepare-release.sh` is bypassed. CLAUDE.md adds a hard rule that all GA-line tags must go through the `release-cycle` skill.
- **In-cycle bumps for the ga.3 cut:** `GSD_PI_VERSION` 2.80.0 → 2.82.0, `OPENCODE_VERSION` 1.14.42 → 1.14.46, Dify 1.14.0 → 1.14.1 (patch — security fix to SECRET_KEY bootstrap + dependency sweep).
- **LLM_ARGS env added** so Cognee passes thinking-mode-off + adequate max_tokens to LiteLLM (closes the 30s hang on cognee pre-flight).

This was originally intended to be the cycle's terminal patch. `v2026.05-ga.4` was added 4 days later as a security-driven follow-up.

### `v2026.05-ga.4` (released 2026-05-17)

Security-driven patch — late-cycle add to address upstream CVEs that landed after `ga.3`. Themes:

- **Authentik 2026.2.2 → 2026.2.3** fixes 1 CRITICAL (reflected XSS in SFE) + 4 HIGH advisories. The headline finding is **GHSA-5wcc** — an unauthenticated forward-auth bypass via the `X-Original-URI` header — which directly applies to our Caddy `forward_auth` topology (every razzfazz.ai box). The hardcoded `authentik-init` migration-hop image moves 2025.12.4 → 2025.12.5 in lockstep (1 HIGH GHSA-h6x7).
- **PostgreSQL CVE-2026-2003/4/5/6 coverage** (three at CVSS 8.8). Floating `postgres:17` tag now resolves to 17.10 on `docker compose pull`. `pgvector/pgvector` pinned from floating `:pg17` to immutable `:0.8.2-pg17` for reproducibility.
- **ClickHouse observability hotfix** — `mem_limit` raised 2 GiB → 6 GiB + `merge_tree.parts_to_throw_insert=300` / `parts_to_delay_insert=150` backpressure. Discovered during prod investigation 2026-05-17: ClickHouse was OOMing under its own `system.text_log` / `system.asynchronous_metric_log` merge backlog (engine self-logging churn, not application telemetry).
- **Broad upstream sweep** — 18 image bumps. OpenHands 1.6.0 → 1.7.0 (compose + sandbox runtime + per-user catalog). Per-user agent images: hermes-agent v2026.5.7 → v2026.5.16, hermes-workspace v2.1.3 → v2.3.0, moltis 20260510.01 → 20260517.01. Paperclip v2026.428.0 → v2026.513.0. opencode 1.14.46 → 1.15.3. Cognee 1.0.9 → 1.1.0. Infisical v0.159.28 (no bump — upstream v0.159.29 reported but does not exist on Docker Hub; v0.159.28 IS the latest stable; deferred). element-web v1.12.17 → v1.12.18. Vespa 8.671.12 → 8.687.75. SearXNG 2026.5.9 → 2026.5.17.
- **edge-tts hygiene pin** — `pip install edge-tts` was unpinned in our custom image; pinned to `==7.2.8`. Flagged on every `check-and-bump-versions` audit since the skill was written.
- **Drift fixes** — valkey compose-default 9.0.3 → 9.0.4, lightrag compose-default v1.4.15 → v1.4.16, `manifests/versions.json` runtime_only entries synced to active env values (moltis, hermes-workspace, openhands runtime).
- **M032 closure** — the test foundation milestone reached 0/0/0 (1114 passed / 0 fail / 0 error / 0 skip / 111 xfail) on the test environment after a long bug-fix campaign + 3-layer ephemeral-postgres leak fix + LLM-variant cycling with restore + wait-for-healthy + BUG-6 cascade-password fix in `core/init-db.sh`.

Deferred to M033 (housekeeping cycle):

- **ClickHouse 24.8.4.13 → 26.x LTS** — major upgrade needs LTS plan + ClickHouse Backup compatibility validation.
- **gsd-pi 2.82.0 → 3.0.0** — breaking major; needs separate validation in `razzfazz-coding-tools`.
- **kyuz0/amd-strix-halo-toolboxes rocm-7.2.1 → 7.2.3** — Strix Halo sensitivity; needs coordinated wrapper-image rebuild + regression test (see `project_strix_halo_kworker_storm` memory).
- **OpenWebUI 0.9.5 has 2 HIGH GHSAs OPEN** with no upstream fix yet — track only.
- **OpenLIT observability pipeline non-functional** — discovered during the ClickHouse hotfix investigation. Every `openlit_*` table is empty or seed-only; no spans land. Three root causes (any/all): (a) `openwebui-pipelines` container has no `openlit_filter.py` loaded; (b) `dify-api` has `OPENLIT_OTLP_ENDPOINT` set but no `OTEL_TRACES_EXPORTER=otlp` to activate the SDK; (c) GPUStack has no OTel instrumentation. The ClickHouse hotfix above stops the OOM symptom but doesn't fix the pipeline. Tracked as **M033-S11**.

### `v2026.05-ga.5` (released 2026-05-25)

Feature + infrastructure patch — the cycle's largest post-GA addition. Themes:

- **Stack-wide MCP (Model Context Protocol) registry (M035)** — a single source of truth (`core/mcp/mcp-servers.yaml`) plus a sync engine wires MCP servers into every consumer consistently. The first server is **Cognee memory** (`remember` / `recall` / `forget`), reachable from **Open WebUI** (native streamable-HTTP MCP), **Dify** (via the proxy-free plugin daemon — keeps SSRF isolation intact), the per-user **Moltis / Hermes / OpenCode** agents, and an **OpenHands** `config.toml` emitter. A `cognee-mcp` sidecar serves it (gated by the `cognee` profile).
- **Automated Ubuntu 24.04 → 26.04 LTS host migration (M034)** — `scripts/razzfazz-upgrade-os.sh` runs the dist-upgrade fully non-interactively (pre-answers the prompts that historically stalled it), stops the stack incl. GPUStack runner pods, and has a post-reboot `--reconcile` phase. 26.04's Canonical-signed kernel (7.0.x) boots under Secure Boot and supports AMD Strix Halo natively — retiring the bespoke mainline-kernel procedure. Customer guide: `docs/upgrade-guide-26.04-lts.md`.
- **Security version bumps** — Caddy `2.11 → 2.11.3` (CVE-2026-30851, forward_auth identity-injection on the SSO gateway path; custom `razzfazz-caddy` image rebuilds), Dify `1.14.1 → 1.14.2` (CVE-2026-41949 cross-tenant file-preview authz bypass + CVE-2026-41948 plugin-daemon path traversal), Gitea `1.26.1 → 1.26.2` (May advisory round). Gotenberg CVE-2026-42589/42596 evaluated but **not** bumped — no fixed upstream tag exists yet (latest 8.32.0).
- **~15 field-deployment bug fixes (M033)** — model names sourced from `standard-models.yaml`; Dify admin-email resolution + fail-loud on zero models; `harden-host.sh` auto-detects the stack-dir owner; upgrade rebuilds newly-added compose services; LightRAG + Cognee admin passwords aligned to the bootstrap password; help center ships both GPUStack v0.7 + v2.1 docs; OpenHands 1.6.0 sandbox env corrected; checksum manager `journal_mode=MEMORY`; Authentik admin-MFA policy binding fixed for 2026.2.x.
- **Append-only GA-tag drift protection** — GA tags are enforced immutable (cut `ga.N+1`, never re-point a pushed tag); `razzfazz-status.sh` records the deployed commit and flags drift vs the tag's current commit.
- **Shared-library consolidation (M033)** — agent-manager uses `razzfazz_common.auth` for Authentik header parsing; the audit logger is promoted into `razzfazz_common.audit_log` (config keeps a thin re-export).

One new env key (COGNEE_MCP_API_KEY, minted post-install on cognee boxes), no breaking changes, no data migration. Requires build (Caddy) + pull (Dify/Gitea); the upgrade script handles both.

Known issues carried/added: coverage gate below threshold on 5 internal modules (pre-existing M032 condition, all functional tests pass); OpenHands MCP config emitter ships but live mount/validation pending; gsd-pi MCP wiring deferred; OpenLIT pipeline still non-functional (M033-S11).

### `v2026.05-ga.6` (released 2026-05-29)

A code/configuration patch hardening single sign-on, the Configuration Portal, and the Dify document-tools integration. No third-party image version bumps. Headline themes:

- **Google + Microsoft Entra ID concurrent SSO.** The two SSO providers can now be enabled at the same time — enabling Entra no longer drops Google from the login page (and vice-versa). The per-provider login-flow blueprints previously overwrote each other's identification-stage source list; they are now generated as a single combined blueprint. Authentik init version → 2.9.
- **Scoped, default-closed Dify→document-tools SSRF exception.** A new `DIFY_DOC_TOOLS_SSRF_ALLOW` env key lets a Dify workflow reach internal document tools (docling / tika / presidio) through the forward-proxy while every other private address stays denied. It defaults to a non-resolving closed sentinel (`disabled.invalid`) — fully closed unless explicitly configured — and the SSRF guardrail test was tightened to permit only this one sanctioned rule.
- **Configuration Portal module/version-management hardening.** Image updates are scoped to the bumped image's own containers; module toggle/update uses `--no-deps` so shared infrastructure isn't recreated; service-name translation with automatic env-revert on a failed update; manifest update-check via the Gitea API; single-box GPU de-duplicated so it no longer shows as two workers.
- **Post-install reconciliation.** Authentik outpost provider bindings and per-user MCP provisioning are reconciled on `razzfazz-post-install.sh --refresh` so forward-auth apps don't return 404 after a provider lands between init runs.
- **Smaller fixes.** presidio-analyzer autoheal (recovers cold-start gunicorn wedges); dify-sandbox `PYTHON_PATH` corrected so code nodes execute; docling ships German OCR (tesseract `deu`).
- **Start-Portal forward-auth routing.** The Start Portal's reverse-proxy block gained the standard `/outpost.goauthentik.io/*` handler (parity with other forward-auth apps).

One new env key (`DIFY_DOC_TOOLS_SSRF_ALLOW`, default closed), no breaking changes, no data migration. Requires build (custom-image source changed); no third-party pulls. Mode A security review for this tag: no exploitable web-exposure findings; image CVE surface unchanged from the prior release (no new images).

---

_Per-cycle release-candidate notes are retained at `releases/<tag>/RELEASE_NOTES.md` for engineering history. The chronological summary lives in `changelog.md` next to this file. This top-level doc is intentionally module-oriented._
