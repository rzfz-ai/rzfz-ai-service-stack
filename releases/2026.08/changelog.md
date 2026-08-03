# Changelog — 2026.08

## v2026.08-ga.9 (2026-08-03)

Reliability / disk-safety patch on ga.8; four fixes, no image-version bumps. `requires_build=true` (agent-manager + start-portal); no new required `.env` keys, no schema-breaking changes.

### Fixed
- **#221 — coding-agent PID limit (2048) reverted to 512 on every (re)provision/restart.** The cap was in the catalog but had no `agent_types` DB column, so it was dropped on the round-trip and the provisioner fell back to docker_client's 512 default. Added `pids_limit` column (schema + migration v9) + upsert plumbing; the startup seed re-upserts each type's value. Re-provision coding agents from **My Agents** post-upgrade.
- **#139 — a runaway per-user agent workspace could make the backup fill the host disk.** `pre-backup.sh` now guards each agent-volume tar: disk-usage ceiling (85%), per-volume size cap (25 GiB), hard `ulimit -f` output cap, sparse tar, partial cleanup. (A live coding workspace ballooned a 2.5 GB volume into a 636 GB tar and took a production box down.)
- **#193/#139 — the observability ClickHouse store grew unbounded and could fill the disk.** Retention now applies at table-create via the OTEL exporter TTL (the init `ALTER` raced table creation and never took effect); adds `MATERIALIZE`, `ttl_only_drop_parts`, a 15-min TTL-merge cadence, a `keep_free_space_bytes=50 GiB` backstop, and a 30 → 7-day default retention.
- **#185 — Start Portal "Manage shortcuts" admin control was a bare link;** now styled as a button matching its siblings.

### Notes
- `requires_build` rebuilds agent-manager + start-portal; `pre-backup.sh` and observability configs are bind-mounted (no rebuild). PDFs regenerated against the ga.9 module set (unchanged vs ga.8).

## v2026.08-ga.8 (2026-08-03)

Single image bump on ga.7: Vaultwarden server **1.36.0 → 1.37.1**. `requires_pull=true`; no env/schema/breaking changes.

### Fixed
- **Bitwarden 2026.7.x extension/desktop showed an empty vault against Vaultwarden.** Clients
  v2026.7.0 are incompatible with Vaultwarden ≤1.36.0 (WASM SDK cipher-deserialization change);
  the web vault + 2026.6.1 clients worked. Vaultwarden 1.37.0+ restores 2026.7.0+ client support.
  Not an encryption/key-rotation issue; no data migration. Verified clean on 0.91 + prod (3472
  ciphers intact; the prod 2026.7.x extension shows the vault again).

## v2026.08-ga.7 (2026-08-02)

Two offline/robustness fixes on ga.6; no image or version-pin change, `requires_build=false`.

### Fixed
- **#184 — air-gapped document extraction hung.** `gen-offline-overlay.py` now adds
  `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` to `docling` + `docling-rq-worker` (previously only
  openwebui/gpustack); docling's per-conversion HuggingFace revision-check SYN-hung behind the
  egress firewall, stalling every doc→JSON conversion. Models are pre-cached; the flags use them.
- **Dify `TRIGGER_URL`** resolves via single-level `https://dify.${MAIN_DOMAIN}` instead of the
  nested `${DIFY_DOMAIN:-dify.${MAIN_DOMAIN}}`, which leaked a literal placeholder into webhook
  URLs after a dify-api recreate.

## v2026.08-ga.6 (2026-08-02)

Single-fix patch on ga.5. `requires_build` rebuilds the `gpustack-legacy` image; no
`.env`, schema, or breaking changes. Only the `llm-legacy` (AMD) profile is affected.

### Fixed
- **Legacy GPUStack worker stuck `not_ready` offline after a container recreate (#127).**
  The image now bakes `fastfetch` 2.25.0.1 into `third_party/bin` and writes `versions.json`
  with GPUStack v0.7.1's builtin version strings (`gguf-parser:v0.22.1`, `fastfetch:2.25.0.1`,
  `llama-box:v0.0.171`), so `prepare_tools()` downloads nothing. Previously it baked a CVE-fixed
  `gguf-parser` but declared `v0.24.1` (builtin is `v0.22.1`) and never baked `fastfetch`; an
  online box downloaded both once and kept them, but the ga.5 offline-package recreate wiped that
  state and an air-gapped worker (WAN blocked by the offline egress firewall) could not re-fetch.
  The skip-check compares only the version string, so the CVE-fixed `gguf-parser` binary is kept.

## v2026.08-ga.5 (2026-08-01)

Multi-fix patch on ga.4 (Vaultwarden + offline hardening + agent caps). `requires_build`
rebuilds the Agent Manager image and recreates Caddy + Vaultwarden. No new active `.env`
keys; no breaking changes.

### Fixed
- **Bitwarden desktop "SSO only" login spun forever; web + mobile worked (#225).** Caddy's
  `(security_headers)` snippet injected `X-Frame-Options: SAMEORIGIN` on every Vaultwarden
  path, including the SSO/2FA connector the desktop client frames → the handshake broke
  (`2FA token not provided`). New `security_headers_vaultwarden` snippet (no `X-Frame-Options`)
  is imported only by the Vaultwarden site; Vaultwarden keeps its own CSP `frame-ancestors`
  clickjacking protection. Ref: dani-garcia/vaultwarden#2111.
- **Vaultwarden ran on SQLite while an unused `vaultwarden_db` sat in Postgres (#226).** Wired
  `DATABASE_URL` + `depends_on: postgres`; existing SQLite data is migrated once by
  `scripts/migrate-vaultwarden-to-postgres.sh` (idempotent, verified parity, fail-clear — never
  orphans data or leaves an empty vault). Fresh + already-migrated boxes are skipped.

### Added
- **`harden-offline-host.sh --egress-firewall` (#184)** — opt-in nftables egress DROP of all
  non-LAN traffic for host + containers (LAN / loopback / multicast / established allowed),
  reboot-persisted, reversible with `--undo`. Makes "offline" kernel-enforced, not just config.
- **Coding-agent PID ceiling raised 512 → 2048 (#221)** for the sandboxed coding family
  (opencode / codex / user-defined); other agent types keep the 512 default.

### Security
- Mode-A review clean — no new Critical/High. #184 is hardening; #225 drops a redundant header
  while retaining Vaultwarden's CSP clickjacking protection; #226 moves data behind the existing
  in-network Postgres (no new exposed port).

### Known issues
- Bitwarden desktop **2026.7.0** shows an empty vault after login (upstream client bug, not the
  box) — use desktop **2026.6.1** (dani-garcia/vaultwarden#7464).

## v2026.08-ga.4 (2026-07-30)

Single-fix patch on ga.3. No version bumps, no new modules, no schema/active-`.env`
changes — code-only rebuild of the Agent Manager image.

### Fixed
- Personal agents: the **Open** button / coding-tools web-terminal port no longer shows
  green before the agent is actually reachable (#220). The readiness probe (`/api/ready`)
  used a bare TCP connect that succeeds the instant the container binds its port; agent
  runtimes (gunicorn/uvicorn/vite/moltis/openhands) listen early but only serve HTTP
  10–60s later, so the button went green while clicking still returned the proxy's
  *"container is not ready yet"* 502. The probe now does a real `httpx.get` — any HTTP
  response = ready, transport error = not-ready — so it agrees with the proxy.

### Security
- Mode-B diff review clean. Same-origin readiness check against the caller's own instance
  container (ownership/auth unchanged, no redirect-follow, body never read) — no new SSRF
  or auth surface. `pre-tag-check`: no security-relevant changes.

## v2026.08-ga.3 (2026-07-29)

Patch on ga.1 (ga.2 folded in — never shipped separately). No version bumps, no new
modules, no schema/active-`.env` changes — code-only rebuild of the Start Portal,
Configuration Portal + Agent Manager images.

### Changed
- **Public distribution channel moved off Codeberg to GitHub**
  (`github.com/rzfz-ai/rzfz-ai-service-stack` — repo, wiki, Releases). Codeberg's 2026-07
  ToU §7 bans heavily-AI-assisted projects. The public remote is centralized
  (`RAZZFAZZ_PUBLIC_REMOTE_DEFAULT` → GitHub, + optional `RAZZFAZZ_PUBLIC_REMOTE_FALLBACK`);
  public/customer boxes auto-repoint `origin` Codeberg→GitHub on upgrade (offline-package
  boxes unaffected); `init` recognizes the GitHub origin (legacy Codeberg still accepted).
  `publish-public` + the community-wiki publisher retargeted to GitHub.

### Security (hardening)
- Genericized bare private example IPs — including an operator infrastructure address —
  in the Mac-gateway sample configs to RFC5737 TEST-NET, so no real infra IP ships in the
  public export.

### Added
- Personal agents: **Restart** button (dashboard card + Settings page) — in-place
  `docker restart`, preserving container/volumes/route/SSO-provider — and a
  Settings-page **Update / Reinstall** control (Update-to-`<version>` when newer,
  else Reinstall/Refresh onto the current image). Both non-destructive (#219).

### Fixed
- Start Portal: logout fully signs the user out — it clears the forward-auth outpost
  proxy session (`/outpost.goauthentik.io/sign_out`) instead of only the Authentik
  core session, which previously left a broken half-logged-in state (the proxy
  re-admitted the user while the IdP session was already gone).
- Config Portal: the Release-Notes / "What's new" dialog scrolls again — the
  background page scroll is locked while a modal is open (with
  `overscroll-behavior: contain`), and the stylesheet is cache-busted per version
  (#216 follow-up).
- Config Portal: changing a personal-agent type's memory limit no longer returns a
  500 — all JSON columns are serialized on save, and a non-JSON upstream error body
  no longer cascades into a second 500.
- Coding agents: "Clone from Gitea" returns an actionable message and the UI states
  the Gitea token needs both `read:user` and `read:repository` (Gitea 1.27 scopes),
  instead of a bare 403.

### Security
- Mode-B diff review clean — no new Critical/High/Medium/Low. The new
  `POST /api/restart/<id>` route enforces the same auth + ownership as its sibling
  lifecycle routes; the coding-agent clone change preserves the credential-exfil
  allowlist. `pre-tag-check`: no security-relevant changes.

## v2026.08-ga.1 (2026-07-28)

Patch release on the GA. No version bumps; agent images rebuilt against the ga.1
set (re-provision from My Agents after `rzfz post-install --refresh`).

### Fixed
- Coding-agent sandboxes no longer exhaust their PID cap — reaping init reaps
  orphaned children (#215).
- Dify model-provider icons render again — the Dify public-URL env is resolved and
  baked into `.env.dify` at provisioning (#160).
- Config Portal: "What's new" dialog scrolls on WebKit/Safari (#216); Network-Policy
  matrix column headers rotated + readable (#217).
- Coding-agent "Clone repository" dialog: optional username field + correct
  `user:token` URL construction, with pre-authed URLs passed through (#213).

### Security
- Hardened the #213 clone anti-exfil allowlist to gate the username credential
  channel (release Mode-B security review finding; commit 68abf2c0). No new
  exposed ports/services/auth keys vs the GA.

### Under the hood
- `USER_DEFINED_APT_PACKAGES` build arg bakes extra system libs into per-user agent
  images at build time; default = Playwright/Chromium runtime libs (#212).
- Init test harness: custom-image scenarios skip cleanly under `--skip-build` when
  built images are absent, instead of a false failure (#214).

## v2026.08-ga (2026-07-27)

Stable GA of the 2026.08 cycle, cut from the validated rc5. Consolidates rc1→rc5.

### Security
- Valkey 9.1.0 → 9.1.1 (CVE-2026-56684 + CVE-2026-63639, both RCE).
- Cycle version sweep (#194/#195/#198): Authentik 2026.5.5, Dify 1.16.0
  (CVE-2026-41948), Gitea 1.27.0 (~43 CVEs), ClickHouse 25.8 LTS, + dify-plugin/
  searxng/cognee/onyx. `CLICKHOUSE_PASSWORD` + `MAC_GATEWAY_MASTER_KEY` auto-minted.
- Accept-residual: LiteLLM CVE-2026-49468 (opt-in Mac-gateway image; internal-only).

### Fixed (rc2→rc5)
- **SSO/identity backlog:** #211 Vaultwarden "verify your email" first login
  (deterministic single email-scope mapping emitting `email_verified: true`, +
  `INIT_VERSION` bump so it reaches upgraded boxes); #183 domain-change blueprint
  re-template; #70 clean-install SSO host; #55 TLS-internal CA drift warning.
- **Upgrade/config robustness:** #177 stale `*_VERSION` reconcile; #176
  Config-Portal orphan guard; #192 per-user agent slug drift; #201 SearXNG verify
  false-negative; #208 postgres PGDATA pinned; #209 offline `--package` no longer
  leaves 153 GB; #210 non-blocking help-cache warm.
- **Field/day-1:** #198 upgrade image-version bumps, #200 OWUI↔GPUStack key, #199
  mic scope, #160 Dify provider icons, #202 docling VLM verify, #203 cognee MCP.

### Validation
Gated rc1→rc5 rollout on 0.91: full init suite + upgrade (single-box/worker ×
online/offline) from v2026.07-ga.10, with real end-to-end verification (chat
answer, SSO login, module UIs) rather than curl-only.

## v2026.08-rc1 (release candidate)

### Added
- **Offline / air-gap (#184):** `RAZZFAZZ_NETWORK_MODE` (online/proxied/offline);
  `rzfz package --include-images --include-models` self-contained package;
  `rzfz verify-images` / `rzfz verify-models` gates; offline local-GGUF model
  provisioning + a "Sideload a Model" Configuration Portal page; offline
  telemetry-off overlay; opt-in reversible `harden-offline-host.sh`; optional
  `RAZZFAZZ_REGISTRY_MIRROR`; an Offline/Network Configuration Portal panel and
  an `rzfz status` OFFLINE category.
- **Mac LLM gateway (#168/#169):** the `mac-llm` profile (LiteLLM → Ollama-on-Mac)
  and a "Mac LLM backends" Configuration Portal panel (add/remove/health + Tier-1
  model management over the Ollama API).
- **Docs portal (#162/#163):** `publish-enterprise-docs.sh` +
  `scripts/lib/confluence_publish.py`; a "How to get support" page; an operator
  provisioning/intake runbook.
- **Observability (#193/#197):** OTLP collector + OpenLIT.

### Fixed
- **#173:** curated public `.gitignore` now excludes `backups/`/`overlay/` — fixes
  the `git stash` upgrade hang on customer boxes with backups.
- **#171:** the Configuration Portal Security page renders the current
  security-architecture doc on customer boxes (via the Enterprise overlay).
- **#159:** the build-only overlay-assembler is stripped from the public export.
- **#184 hardening:** `rzfz package --include-images` stages on a large disk (never
  tmpfs) with a free-space preflight — no more silently-truncated offline packages.

### Changed
- Version currency: Dify 1.16.0 (CVE-2026-41948), Authentik 2026.5.5, Cognee
  1.4.0, Onyx v4.3.9, base-image refreshes.
- `RAZZFAZZ_CORPORATE_PROXY` / `RAZZFAZZ_OFFLINE` are now internals of
  `RAZZFAZZ_NETWORK_MODE` (back-compat migrated on upgrade).
