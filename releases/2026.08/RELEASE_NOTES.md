# Release Notes — 2026.08

**Cycle status:** GA — latest `v2026.08-ga.5` (2026-08-01); `v2026.08-ga.4` (2026-07-30);
`v2026.08-ga.3` (2026-07-29); `v2026.08-ga.1` (2026-07-28); `v2026.08-ga` (2026-07-27). (ga.2
was folded into ga.3, never shipped separately.) Shipped after a gated rc1→rc5 rollout
validated on the 0.91 reference box.

See `WHATS_NEW.md` for the cycle themes and `changelog.md` for the change list.
Per-tag notes: `releases/2026.08-ga.5/`, `releases/2026.08-ga.4/`, `releases/2026.08-ga.3/`, `releases/2026.08-ga.1/`, `releases/2026.08-ga/`.

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
_Known issue (upstream, not the box): Bitwarden desktop **2026.7.0** shows an empty vault
after login — use desktop **2026.6.1** until upstream fixes it._

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
