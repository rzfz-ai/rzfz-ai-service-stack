# Release Notes — 2026.08

**Cycle status:** GA — latest `v2026.08-ga.12` (2026-08-13); `v2026.08-ga.11` (2026-08-06); `v2026.08-ga.10` (2026-08-05); `v2026.08-ga.9` (2026-08-03); `v2026.08-ga.8` (2026-08-03); `v2026.08-ga.7` (2026-08-02);
`v2026.08-ga.6` (2026-08-02); `v2026.08-ga.5` (2026-08-01); `v2026.08-ga.4` (2026-07-30);
`v2026.08-ga.3` (2026-07-29); `v2026.08-ga.1` (2026-07-28); `v2026.08-ga` (2026-07-27).
(ga.2 was folded into ga.3, never shipped separately.) Shipped after a gated rc1→rc5
rollout validated on the 0.91 reference box.

See `WHATS_NEW.md` for the cycle themes and `changelog.md` for the change list.
Per-tag notes: `releases/2026.08-ga.12/`, `releases/2026.08-ga.11/`, `releases/2026.08-ga.9/`, `releases/2026.08-ga.8/`, `releases/2026.08-ga.7/`, `releases/2026.08-ga.6/`, `releases/2026.08-ga.5/`, `releases/2026.08-ga.4/`, `releases/2026.08-ga.3/`, `releases/2026.08-ga.1/`, `releases/2026.08-ga/`.

## Patch — v2026.08-ga.12 (2026-08-13)
Corporate TLS-intercept-proxy + upgrade-hardening bugfix on ga.11; no image-version
bumps, no config/schema migration, no breaking changes. Fixes the class of failures
surfaced by the first customer box behind a re-signing corporate web proxy:
comprehensive internal-bypass (NO_PROXY) coverage across all profiles + the host
gateway; a **Dify marketplace SSRF chaining sidecar** (keeps the internal RFC1918
deny-list intact, routes public egress via the corporate proxy — allowlist
`marketplace.dify.ai`); agent-provisioner proxy + CA injection for runtime-spawned
containers; channel-aware config-portal version-manifest fetch; cognee/matrix/tika
loopback-healthcheck env-clear (no false unhealthy behind a daemon proxy); GPUStack
schema-line guard + loopback embedded-worker IP; cognee-frontend same-origin API base;
`rzfz setup` COMPOSE_PROFILES preservation; offline-package version-stamp + `rzfz
status` honesty; model-registry (distroless Zot) healthcheck fix. On proxied boxes the
upgrade auto-regenerates the corporate-proxy overlay. Per-tag notes:
`releases/2026.08-ga.12/`.

## Patch — v2026.08-ga.11 (2026-08-06)
Security hotfix on ga.10; four clean drop-in image bumps, `requires_pull`, no config/schema
changes. **Gitea 1.27.0 → 1.27.1** fixes **CVE-2026-59774 (CVSS 9.8)** — unauthenticated
arbitrary file read via the Org-Mode `#+INCLUDE` renderer, with an in-the-wild cryptominer
campaign reported against the `1.27.0-rootless` image (highest-priority). **Synapse
v1.156.0 → v1.158.0** + **Element Web v1.12.23 → v1.12.25** pick up security release 1.157.2
(6 High + 3 Moderate advisories); verified safe — this stack uses classic `oidc_providers`
(not MSC3861). **LightRAG v1.5.4 → v1.5.5** closes ~8 advisories incl. an auth-independent
Stored-XSS (GHSA-xpjq-3w4w-w5wr), a DoS, an IPv6-SSRF and CVE-2026-61808 — no embedding/schema
change, so no re-index. Gitea + LightRAG are `.env`-pinned (change_default migration bumps
them on upgrade); Synapse + Element Web are hardcoded in compose. hermes-agent v2026.7.1 → v2026.8.3 (security: token-leak + DoS/ReDoS/JWKS) is
rebuilt from source and included, gated on a passing 0.91 Option-B boot re-validation. Mode-B security review.

## Patch — v2026.08-ga.10 (2026-08-05)
Security + configuration hotfix on ga.9; no image-version bumps except the `cryptography`
pin. **SSO login on fresh installs is fixed (OIDC `grant_types`):** Authentik 2026.5.5 made
`grant_types` a required provider field, and the four native-OIDC blueprints (Open WebUI,
Gitea, Synapse/Matrix, Vaultwarden) did not set it — so a provider created fresh under 2026.5.5
got an empty list and rejected every SSO authorize as `invalid_request`, breaking "Log in with
SSO" on new installs. All four blueprints now set `grant_types: [authorization_code,
refresh_token]`, with `INIT_VERSION` 3.4 → 3.5 so the fix re-applies on upgrade. (Boxes upgraded
from an earlier release with a long-lived Authentik database kept their populated defaults and
were unaffected.) **`cryptography` 46.\* → 50.\*** clears three Dependabot advisories (PKCS#7
Bleichenbacher oracle [High], duplicate self-signed-intermediate path-building [High], wildcard
`permittedSubtrees` escape [Moderate]). **Public-channel upgrade reliability:** `rzfz upgrade
--update` no longer fails "Checksum mismatch!" (the published `versions.json.sha256` is
regenerated to match its manifest), and the release publisher now creates the GitHub tag +
Release for every cut so `--target` upgrades resolve; the `versions.json` header +
`VERSIONS.md` are refreshed (Vaultwarden 1.37.1) and the manifest path corrected to
`config/manifests/`. The Dify example `SECRET_KEY` placeholder is scrubbed (never a live key).
After upgrade, to pick up the persisted 2048-PID cap re-provision coding agents from **My
Agents** or use the agent Settings **Update / Reinstall** button (a plain restart does not apply
it). Mode-A security review clean.

## Patch — v2026.08-ga.9 (2026-08-03)
Reliability / disk-safety patch on ga.8; four fixes, no image-version bumps,
`requires_build=true` (agent-manager + start-portal rebuilds). **The coding-agent
PID limit is now persisted (#221):** the 2048 cap was defined in the catalog but had
no column in the `agent_types` table, so it was dropped on the DB round-trip and every
(re)provision reverted to the 512 default — busy coding sessions hit *"Resource
temporarily unavailable"*; a `pids_limit` column + migration v9 + upsert plumbing make
it stick. **The per-user agent backup can no longer fill the host disk (#139):**
`pre-backup.sh` guards each agent-volume tar with a disk-usage ceiling, a per-volume
size cap, a hard `ulimit -f` output cap, sparse-aware tar, and partial-tar cleanup —
after a live coding-agent workspace ballooned a 2.5 GB volume into a 636 GB tar and
filled a production disk. **The observability ClickHouse store now applies retention
reliably and cannot fill the disk (#193/#139):** the OTEL exporter bakes the TTL at
table-create (the old init-time `ALTER` raced table creation and never took effect, so
the store grew unbounded), with `ttl_only_drop_parts`, a prompt TTL-merge cadence, a
`keep_free_space_bytes` disk backstop, and a 30 → 7-day default retention. **The Start
Portal admin "Manage shortcuts" control renders as a button (#185)** instead of a bare
link. After upgrade, re-provision coding agents from **My Agents** to pick up the
persisted PID cap. Mode-B security review clean.

## Patch — v2026.08-ga.8 (2026-08-03)
Single image bump on ga.7: **Vaultwarden 1.36.0 → 1.37.1**, restoring compatibility with
**Bitwarden 2026.7.0+ clients**. Bitwarden's 2026.7.x browser-extension and desktop apps
changed their WASM SDK cipher-deserialization in a way that is incompatible with
Vaultwarden ≤1.36.0 — the client logs in but shows an **empty vault** (autofill unusable),
while the web vault and older 2026.6.1 clients keep working. Vaultwarden 1.37.0 is *"required
for support with clients version 2026.7.0+"* (upstream); 1.37.1 is the current patch. Not an
encryption / key-rotation issue and no data migration — the upgrade recreates `vaultwarden`
and runs its own schema migration (verified clean on 0.91 + prod, 3,472 ciphers intact,
prod 2026.7.x extension shows the vault again). `requires_pull=true`; no `.env`, schema, or
breaking changes. Mode-B review clean.

## Patch — v2026.08-ga.7 (2026-08-02)
Two offline/robustness fixes on ga.6; no image or version-pin change. **Air-gapped
document extraction no longer hangs:** the offline overlay generator was giving
`HF_HUB_OFFLINE` to openwebui + gpustack but **not to docling**, so on a sealed box
docling's per-conversion HuggingFace revision-check SYN-hung behind the egress firewall
and stalled every doc→JSON extraction before any model ran; docling is now in the offline
flag list (its models are pre-cached, so it uses them directly) (#184). **The Dify
webhook-trigger URL no longer shows a literal `${MAIN_DOMAIN}` placeholder:** `TRIGGER_URL`
resolves via single-level `https://dify.${MAIN_DOMAIN}` instead of the nested
`${DIFY_DOMAIN:-dify.${MAIN_DOMAIN}}`, which a recreated `dify-api` could leave unresolved.
`requires_build=false`; the upgrade regenerates the offline overlay (offline boxes) and
recreates docling + dify-api. No `.env`, schema, or breaking changes. Mode-A review clean.

## Patch — v2026.08-ga.6 (2026-08-02)
Single-fix patch on ga.5. **Air-gapped AMD boxes no longer get a stuck GPUStack
worker after an offline-package upgrade.** The legacy GPUStack image (`llm-legacy`
profile, AMD Strix Halo) now bakes the `fastfetch` helper into the image and writes
`versions.json` with GPUStack v0.7.1's builtin version strings, so the worker's
`prepare_tools()` skips every download instead of re-fetching `fastfetch` +
`gguf-parser` on start. Previously an online box downloaded those two tools once and
kept them in the container, but the ga.5 offline-package container-recreate wiped that
state — and with WAN blocked by the offline egress firewall the worker could not
re-fetch them, so it stuck at `not_ready` with no models (#127). The download-skip
check compares only the version string, so the CVE-fixed `gguf-parser` binary is
retained (no CVE regression). `requires_build=true` (rebuilds the `gpustack-legacy`
image); no `.env`, schema, or breaking changes; only the `llm-legacy` (AMD) profile is
affected. Mode-A security review clean.

## Patch — v2026.08-ga.5 (2026-08-01)
Multi-fix patch on ga.4, grouped around **Vaultwarden + offline hardening**. **The Bitwarden
desktop "SSO only" login no longer hangs on an endless spinner** — Caddy stopped injecting
`X-Frame-Options` onto Vaultwarden's SSO/2FA connector (which the desktop client frames);
Vaultwarden keeps its own CSP `frame-ancestors` clickjacking protection (#225).
**Vaultwarden now stores its data in the shared PostgreSQL** instead of a stray SQLite file,
so the vault is part of the standard Postgres backup path; existing installs are migrated
once, automatically and idempotently, on upgrade (verified row-count parity, SQLite kept as
rollback, fail-clear so a vault is never left empty) (#226). **Offline boxes can now be sealed
by the kernel:** `harden-offline-host.sh --egress-firewall` drops all non-LAN egress for both
host and containers via nftables (reboot-persisted, `--undo` reverses) — closing the gap that
offline *mode* alone only gates application egress, not the network (#184). Sandboxed coding
agents are lifted from the 512-PID hardening default to **2048** so heavy agentic sessions
(MCP servers + multiple agent processes) stop hitting `Resource temporarily unavailable`
(#221). Also fixes the shipped **Shortcuts** admin: it is now reachable from a "Manage
shortcuts" link in the Start Portal and a Shortcuts item in the Config Portal nav (was
URL-only), renders on a readable panel (was on the background image), and gains a group-list
filter (#185; an Open WebUI model picker is still to come). `requires_build` rebuilds the
agent-manager + start-portal images and recreates Caddy + Vaultwarden; no new active `.env`
keys, no breaking changes. Mode-A security review clean.
_Known issue (ga.5, now **resolved in ga.8**): Bitwarden **2026.7.0+** clients showed an empty
vault against Vaultwarden ≤1.36.0; ga.8's Vaultwarden **1.37.1** bump restores compatibility —
no client downgrade needed._

## Patch — v2026.08-ga.4 (2026-07-30)
Single-fix patch on ga.3. **Personal agents: the "Open" button / coding-tools web-terminal
port now goes green only once the agent is actually reachable** — not the instant its
container starts (#220). The readiness probe (`/api/ready`) used a bare TCP connect that
succeeds as soon as the container binds its port, but agent runtimes
(gunicorn/uvicorn/vite/moltis/openhands) listen early and only serve HTTP 10–60s later —
so the button flipped to green while clicking it still returned the reverse-proxy's
*"Agent starting… container is not ready yet"* (502) on every session. The probe now issues
a real `httpx.get` on the container root: any HTTP response = ready, a transport error
(connect refused / timeout / reset) = not-ready. `requires_build` rebuilds the agent-manager
image on upgrade; existing agents/instances are untouched (no re-provision). No version
bumps, no schema or `.env` changes. Mode-B security review clean.

## Patch — v2026.08-ga.3 (2026-07-29)
**Public distribution moved off Codeberg to GitHub** (`github.com/rzfz-ai/rzfz-ai-service-stack`
— repo, wiki, and Releases): Codeberg's 2026-07 Terms of Use (§7) now prohibit projects
that mostly consist of generative-AI-written code, so the community/customer channel was
moved. Public/customer boxes **auto-repoint** `origin` Codeberg→GitHub on this upgrade
(anonymous pulls; offline-package boxes are unaffected); the public remote is centralized
with an optional fallback, and `init` recognizes the GitHub origin (legacy Codeberg still
accepted during the transition). A hardening bonus genericized bare private example IPs
(including an operator infrastructure address) in the Mac-gateway sample configs to
documentation (TEST-NET) IPs. This release also folds in everything staged as ga.2:

Four fixes found running ga.1 on production, plus one personal-agent feature. No
version bumps, no new modules, no schema or `.env` changes — a code-only rebuild of
the Start Portal, Configuration Portal, and Agent Manager images. **Start Portal
logout now fully signs you out** — it clears the forward-auth proxy session instead
of only the Authentik core session, fixing a broken half-logged-in state. The Config
Portal Release-Notes /
"What's new" dialog scrolls again (the mouse wheel now scrolls the dialog, not the
page behind it — the real fix was a background scroll-lock, plus per-version
stylesheet cache-busting; #216 follow-up); changing an agent type's memory limit no
longer returns a 500 (all JSON columns are serialized on save); and "Clone from
Gitea" states that the token needs both `read:user` and `read:repository` instead of
a bare 403. New: personal agents gain a **Restart** button and a Settings-page
**Update / Reinstall** control (#219), both non-destructive (chats/skills/files/config
preserved). Mode-B security review clean.

## Patch — v2026.08-ga.1 (2026-07-28)
Seven targeted fixes on the GA; no version bumps, no new modules, one new
(non-secret) `.env` key (`USER_DEFINED_APT_PACKAGES`, migration-tracked,
`requires_build`). Coding agents no longer die from PID exhaustion (#215); Dify
model-provider icons render again (#160); the Config Portal "What's new" dialog
scrolls on WebKit and the Network-Policy matrix headers are readable (#216/#217);
the coding-agent clone dialog takes a username and handles tokens correctly
(#213 — with its anti-exfil allowlist hardened to gate the username channel per
the release security review). Under the hood: a `USER_DEFINED_APT_PACKAGES` build
knob bakes system libs into per-user agent images (#212) and an init-suite
skip-pre-check (#214, test-only). Agent images are rebuilt on upgrade — re-provision
from **My Agents** after `rzfz post-install --refresh`.

## Cross-cutting themes
1. Offline / air-gapped operation (#184) — the flagship.
2. Apple-silicon Mac LLM gateway (#168/#169).
3. Enterprise documentation portal (#162/#163).
4. Customer-box delivery reliability (#171/#173/#159).
5. Stack-wide LLM observability (#193/#197).
6. Platform currency + security sweep (Dify/Authentik/Gitea/ClickHouse/Cognee/Onyx + base images; Valkey 9.1.1 RCE fix).

## GA hardening (rc4 → rc5)
Before the GA the full genuinely-open defect list was cleared and re-verified
end-to-end on 0.91: Vaultwarden SSO first login (#211 — deterministic
`email_verified` that also reaches upgraded boxes), domain-change SSO re-template
(#183), clean-install SSO host (#70), TLS-internal CA drift warning (#55), upgrade
`*_VERSION` reconcile (#177), Config-Portal orphan guard (#176), per-user agent
slug drift (#192), SearXNG verify false-negative (#201), plus the offline-upgrade
`images/` leftover (#209) and non-blocking help-cache warm (#210).

## Upgrade path
Standard `rzfz upgrade` from v2026.07-ga.10 (online or offline `--package`).
Pre-2026.07 boxes cross via the reorg-aware bootstrap. All cycle `.env`/image
changes apply automatically via the migration manifest (rc1→ga blocks); the
Authentik blueprint re-apply rides an `INIT_VERSION` bump (no image rebuild).
