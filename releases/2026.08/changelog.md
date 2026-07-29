# Changelog — 2026.08

## v2026.08-ga.2 (2026-07-29)

Patch on ga.1. No version bumps, no new modules, no schema/`.env` changes —
code-only rebuild of the Configuration Portal + Agent Manager images.

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
