## What's new in 2026.05

Headline changes — see Release Notes for the full module-by-module breakdown.

- **Reliable backups & disaster recovery (`ga.7`).** A regression had quietly left the database, environment configuration, and agent state out of every backup, and restores onto a clean install would fail to start. Both are fixed, and a new automated test guards the full backup → wipe → restore cycle. **Important:** take a fresh backup after upgrading — backups made before `ga.7` are incomplete and cannot be restored.
- **Google + Microsoft Entra ID sign-in together (`ga.6`).** Both SSO providers can now be enabled at once — turning on Entra no longer hides the Google button (or vice-versa). Also in `ga.6`: a default-closed scoped exception lets Dify reach internal document tools (docling/tika/presidio) without weakening the SSRF proxy, plus a steadier Configuration Portal (module toggles and image updates only touch the services they should).
- **Stack-wide MCP + shared agent memory (`ga.5`).** A central MCP registry wires Model Context Protocol servers into every consumer at once; the first is **Cognee memory** (`remember`/`recall`/`forget`), now usable from Open WebUI, Dify, and the per-user Moltis / Hermes / OpenCode agents.
- **One-command Ubuntu 26.04 LTS host upgrade (`ga.5`).** `razzfazz-upgrade-os.sh` runs the 24.04 → 26.04 migration unattended and brings the stack back up; the Canonical-signed 7.0 kernel re-enables Secure Boot and supports AMD Strix Halo out of the box.
- **Personal Agent Manager — per-user.** Hermes / Moltis / Coding Tools / OpenHands / Paperclip are now provisioned per Authentik user from the new "**My Agents**" drawer. Replaces the stack-global agent containers from 2026.04. Hermes + Moltis now built locally from upstream source (immutable provenance).
- **razzfazz.ai Start Portal.** New tile-based landing page at `start.<your-domain>` with status, RAM usage, country-of-origin flag, and click-through per module. Per-user pinning, drag-and-drop reorder, custom categories, multi-column responsive layout.
- **Authentik 2025.10 → 2026.2.3** in one upgrade — three init containers handle the schema migration with no operator schema steps. (2.2 at GA; 2.3 in `ga.4` closes a CRITICAL XSS + the GHSA-5wcc forward-auth bypass.)
- **Dify 1.13.3 → 1.14.1** with the LiteLLM 1.83.0 supply-chain fix (CVE-2026-35030 closed). (1.14.0 at GA; 1.14.1 in `ga.3`.)
- **Host kernel + ROCm freeze broken.** The kernel-6.14 / ROCm-6.4 hold from previous cycles is gone — operators can now run modern OEM kernels (`linux-oem-24.04d` / `6.17`+) and ROCm 7.2 (in-tree `amdgpu`, no DKMS). **GPUStack itself stays on the validated v0.7.1 + custom Vulkan build as the recommended STABLE default**; upstream v2.1.x is added as opt-in EXPERIMENTAL via the new Configuration Portal → Modules → LLM Runtime toggle, but production AMD and CPU installs default to v0.7.1 after a 12-hour stop/start soak surfaced a slow leak on v2.1.x.
- **One-click runtime flip.** Configuration Portal → Modules → LLM Runtime switches between STABLE (v0.7.1) and EXPERIMENTAL (v2.1.x) without touching files. Handles `.env` flip, container teardown, orphan v2.x runner pod cleanup, gpustack_db schema reset, API-key rotation, and custom-backend re-registration — streamed live.
- **OpenWebUI ↔ Dify Manifold Pipe.** Route any OpenWebUI chat through a Dify workflow via `DIFY_APPS_JSON` + `dify/seed-apps.sh`.
- **New module: Crawl4AI** — RAG-friendly web crawler. Pairs with SearXNG for content fetch into Dify / LightRAG / Cognee / Onyx knowledge bases.
- **Cognee Next.js frontend.** The bare FastAPI Swagger landing page is replaced by the upstream Next.js frontend, Caddy splits routing on the cognee subdomain.
- **Stack-wide observability** — new `observability` profile (OpenLIT + ClickHouse) ingests OpenTelemetry traces from OpenWebUI / Dify / agents.
- **Image bumps across the stack (full-cycle).** Synapse v1.152.1 (DoS + pagination GHSAs), Vaultwarden 1.36.0 (4 named SSO/SSRF fixes), Valkey 9.0.4 (3 CVEs), Open WebUI 0.9.5, Komodo 2.2.0, Infisical v0.159.28, Element Web v1.12.18, LightRAG v1.4.16, Stirling-PDF 2.10.1, Docling v1.18.0, SearXNG 2026.5.17-f26e45077, paperless-ngx 2.20.15, Onyx v3.2.12, OpenHands 1.6.0 → 1.7.0, Vespa 8.671.12 → 8.687.75. Per-user agents: Hermes-Workspace v2.3.0, Moltis 20260517.01, OpenCode 1.15.3, gsd-pi 2.80.0 → 2.82.0; Hermes-Agent v2026.5.16 closes 8 P0s including a CVSS 8.1 Discord allowlist scoping (line started at v2026.5.7 "Tenacity Release" at GA, bumped to v2026.5.16 in `ga.4`). Custom builds: cognee 1.0.1 → 1.1.0 (1.0.9 mid-cycle, then → 1.1.0 in `ga.4`; upstream skipped 1.0.10), Paperclip v2026.428.0 → v2026.513.0, dify-web tracks `DIFY_VERSION=1.14.1` (vendored `web/` still ships upstream's 1.11.2 `package.json`; bundled 1.14.x build at runtime). Observability: OpenLIT 1.18.1 → 1.20.0. Gotenberg 8.32.0 with `--chromium-deny-private-ips` re-enabled (8.32 reverted the strict default). Security pins added in `ga.4`: PostgreSQL `:17` floating tag picks up 17.10 (CVE-2026-2003/4/5/6, three at CVSS 8.8), pgvector pinned to immutable `:0.8.2-pg17`, edge-tts pinned to `==7.2.8` (was unpinned). Nuclei outside scan: **0 matches** (62 hosts × 6162 templates × 15k+ requests).
- **Late-GA security mitigations.** Authentik GHSA-qvxx-mfm6-626f (Critical, CVSS 9.1, Authenticated RCE in Policy/Property Mapping test endpoint) remains unpatched on the 2026.2.x line as of `ga.4`'s 2026.2.3 bump — the Caddy edge-block on the affected paths stays in place until a future upstream 2026.2.x release ships the fix. Dify echarts DOM XSS (no upstream patch) covered by `Content-Security-Policy-Report-Only` on `dify.<domain>`; reports POST to `/csp-report`. The App-library slim-down reworked mid-window via `meta_launch_url=blank://blank` after the initial delete-the-Application approach broke forward-auth — Authentik App library now shows only `start`+`help`+`licenses`. Five real bugs in `razzfazz-upgrade.sh` and `apply-policy-bindings.py` caught + fixed during fleet validation: force-rebuild on commit-move, fail-fast on non-interactive sudo, bootstrap fetch refspec under `refs/tags/`, poll-until-stable before outpost attach, verify-and-reattach loop after `outpost_controller` clobbers concurrent writes.
- **Backup pipeline encrypted end-to-end.** GPG encryption reflected across the dashboard, Backup management UI, Management UI Backup section, and CLI. Encrypted backups are restorable. Env-snapshots from `migrate_env` surfaced in a new panel.
- **Stability sidecar + sysctl.** `autoheal` watches Docker `unhealthy` state across the stack; kernel sysctl tunable `vm.panic_on_oom=0` replaces the earlier `=1` panic-and-reboot default with kill-largest-process (less aggressive on tight-memory boxes). _Post-GA hotfix:_ the `autoheal=true` label has been removed from all three GPUStack variants — autoheal was killing gpustack mid-storm during model first-load on Strix Halo and triggering reload cascades. 100-min sustained-inference soak validated leak-free without it.
- **Operator UX.** Management UI dashboard / modules / help / licenses / backup all carry data-driven listings (no more hardcoded module lists drifting from reality). New `--refresh` post-install mode for safe post-upgrade re-config. Bootstrap script for big-bang upgrades from 2026.04-ga.

**6 of 7 audit findings closed by GA**, including the gpustack worker-port LAN-leak (master-mode `0.0.0.0` bind no longer applied in standalone mode). The two remaining items — gpustack v2.1.x bundled-dependency CVE inheritance (only relevant on the EXPERIMENTAL opt-in path) and autoheal Alpine base CVE debt (fix planned for the next cycle) — are documented as accept-residual / planned-fix.

_See the Release Notes link in the nav for the full module-by-module change log._

---

## Patch line: ga.1 → ga.4

The GA-day hotfix series tagged and shipped as `ga.1` + `ga.2` (both 2026-05-13), with `ga.3` (cycle-closing, 2026-05-13) and `ga.4` (security follow-up, 2026-05-17) added subsequently.

### `2026.05-ga.1` highlights

- **Caddy network-alias refactor** — `${AUTHENTIK_DOMAIN}` + `${MATRIX_DOMAIN}` become Docker network aliases on the caddy container; retires `CADDY_IP` / `extra_hosts`. Vaultwarden + matrix now survive Caddy restarts cleanly.
- **LLM single source of truth** — `core/llm/standard-models.yaml` is the one place LLM identities (alias, quant, ctx-size, parallel) live. `core/llm/sync.py` reconciles into every consumer; the operator-facing `propagate-llm-config` skill documents the canonical 9-touchpoint propagation order.
- **Patched llama.cpp Vulkan build** — `llm/gpustack/patched-llama-cpp/` is a reproducible build of llama.cpp b9112 with `ggml-org/llama.cpp#22458` applied. Fixes the gemma4 second-image SWA prompt-cache crash on AMD Strix Halo. Binaries are not committed (build.sh + deploy.sh + 22458.patch + README).
- **Agent-provisioning hardening** — seven follow-up source fixes from the overnight 2026-05-12/13 session: upgrade self-heal, agent-manager `catalog.py` bind-mount fix, `docker exec -u uid` correctness, OpenHands subdomain fix, paperclip auto-allowlist, Caddy stale-ACME-lock cleanup, Caddy broken-cert self-heal.
- **OpenWebUI** `0.9.4 → 0.9.5`, **gotenberg** `8.31.0 → 8.32.0` (8.31 crash-loops on the `--chromium-deny-private-ips` flag).
- **Dify SMTP 1.14 fix** — `SMTP_LOCAL_HOSTNAME=dify-api` (was empty, broke postfix HELO on Dify 1.14 only).

### `2026.05-ga.2` highlights

- **Dify password reset works again.** Filed upstream as [`langgenius/dify#36116`](https://github.com/langgenius/dify/issues/36116) with the fix in [`langgenius/dify#36117`](https://github.com/langgenius/dify/pull/36117). Until that lands and we move to a Dify release that includes it, a small entrypoint wrapper (`dify/patches/entrypoint-wrapper.sh`) adds the missing `phase: str` field to `_TokenData` at container start.
- **Reset token: 30 minutes** (up from Dify's 5-minute default). Long enough for the email to arrive.
- **Login lockout: 30 minutes** (down from Dify's 24-hour default). One fat-finger sequence no longer locks a user out until tomorrow.

`migrations/env-changes.json`: 2 new entries (`2026.05-ga.1`, `2026.05-ga.2`). No new operator-set secrets — every env change is `add` with a default or `change_default`.

### `2026.05-ga.3` highlights (released 2026-05-13)

The originally-planned cycle-closing patch. Per-user agent state is included in the backup pipeline; every LLM-config consumer (catalog.py / coding-tools / razzfazz-post-install / opencode / hermes / moltis / gpustack-deploy / lightrag / cognee) now reads `standard-models.yaml` directly — closing the consumer-side adoption that `ga.1` started. Plus security review (Mode A) + pre-tag tripwire + customer handover doc rewrite.

### `2026.05-ga.4` highlights (released 2026-05-17)

The actual cycle-closing patch — late-cycle add for upstream CVEs that landed after `ga.3`. **Recommended upgrade for every box.**

- **Authentik security upgrade** 2026.2.2 → 2026.2.3 fixes a CRITICAL XSS and four HIGH advisories — most notably **GHSA-5wcc**, an unauthenticated forward-auth bypass via the `X-Original-URI` header that directly applies to our Caddy `forward_auth` topology (every razzfazz.ai box).
- **PostgreSQL CVE coverage** — `postgres:17` floating tag picks up 17.10 with four CVEs patched (three at CVSS 8.8). pgvector pinned to immutable `0.8.2-pg17`.
- **OpenHands minor bump** 1.6.0 → 1.7.0 across both the global compose path and per-user catalog.
- **Per-user agent image refresh** — Hermes v2026.5.7 → v2026.5.16, Hermes-Workspace v2.1.3 → v2.3.0, Moltis 20260510.01 → 20260517.01, Cognee 1.0.9 → 1.1.0, Paperclip v2026.428.0 → v2026.513.0, opencode 1.14.46 → 1.15.3.
- **ClickHouse observability hotfix** — raises `mem_limit` 2 GiB → 6 GiB + adds merge backpressure. Discovered during a prod investigation: ClickHouse was OOMing under its own internal merge backlog under any sustained activity.
- **Drift hygiene** — valkey/lightrag compose defaults aligned with `.env.example`, edge-tts unpinned dependency pinned to `==7.2.8`, manifest runtime_only entries synced to active env values.

Known issue ga.4 ships with: the OpenLIT observability UI shows no data (the writer pipeline — OWUI filter, Dify OTel SDK, GPUStack instrumentation — isn't emitting). The ClickHouse fix above stops the OOM symptom but doesn't fix the underlying pipeline; tracked as the next cycle backlog for the next cycle.

After ga.4, the `2026.05-ga` line is done and we move to `2026.06`.
