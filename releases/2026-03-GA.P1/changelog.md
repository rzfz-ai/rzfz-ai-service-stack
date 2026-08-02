# 🔧 razzfazz.ai — Release 2026.03-GA.P1

**Patch 1 — Stability, SMTP & Upgrade Resilience**

*March 2026 · 5 commits · Post-GA hardening*

---

Following the General Availability release, this first patch addresses real-world deployment feedback: SMTP compatibility with Dify's email plugin, Authentik authentication bypass for Dify webhooks, improved Google Workspace mail delivery, and a fundamentally reworked upgrade strategy that preserves manual Authentik UI customizations.

> **Naming note:** The previous release tag `2026-03.GA` had dot and hyphen transposed. Starting with this release, we follow the correct CalVer convention: `YYYY.MM-GA.Pn`. The upgrade script now handles both formats transparently.

---

## ✨ Highlights

### 📬 SMTP Auth for Dify Email Plugin
The Dify email plugin requires SMTP authentication — no exceptions, even for trusted internal relays. The SMTP relay container now ships with SASL AUTH enabled out of the box, using a static internal credential (`mail@<domain>` / `mailpass`). Since the relay is only reachable from the Docker network, this is safe by design and eliminates the #1 support issue with Dify email workflows.

### 🔄 Upgrade Strategy: Blueprints → Data Migrations
Blueprint re-sync during upgrades was a ticking time bomb — it silently overwrote manual changes made in the Authentik Admin UI (branding, flow customizations, group policies). This release replaces `sync_blueprints()` entirely with **targeted Django ORM data migrations** that surgically update only what's needed. Blueprints now only run during first-time init.

### 🌐 Dify Webhook Passthrough
Dify's `/triggers/*` endpoint for incoming webhooks was blocked by Authentik's forward-auth proxy. Webhooks are now routed directly to the Dify API, bypassing SSO — because external systems calling your webhooks can't log into Authentik first.

---

## 🆕 What Changed

### SMTP & Email
- **SASL AUTH on SMTP relay** — new `enable-sasl.sh` script installs Cyrus SASL and configures a static credential pair, enabling Dify's email plugin to authenticate against the local relay
- **SMTP entrypoint hardened** — stale Postfix PID files from unclean shutdowns are cleaned up on container start, preventing startup crashes
- **Google Workspace SMTP relay support** — default relay host changed from `smtp.gmail.com` to `smtp-relay.gmail.com` for IP-based auth; documented both options
- **Configurable sender domains** — new `SMTP_ALLOWED_SENDER_DOMAINS` variable allows sending from domains other than `MAIN_DOMAIN`

### Dify & Caddy
- **Webhook bypass** — `/triggers/*` path excluded from Authentik forward-auth and routed directly to `dify-api:5001`
- **Dify email tool** — works out of the box with local SMTP relay using `mail@<domain>` / `mailpass` credentials

### Upgrade System
- **Blueprint sync removed** — `sync_blueprints()` replaced by `run_data_migrations()` to preserve manual Authentik UI changes
- **Data migration framework** — version-gated Django ORM scripts run inside `authentik-worker`, with health-check polling, progress logging, and manual recovery hints
- **First migration** — sets `access_token_validity=hours=24` on all Authentik proxy providers (prevents premature session expiry on GPUStack and other long-lived connections)
- **CalVer regex fix** — version parser now handles both `YYYY.MM-GA` and `YYYY-MM.GA` formats (case-insensitive)
- **Pipefail fix** — `read_env_value()` no longer crashes on pre-versioning installations missing a `VERSION` file
- **Authentik-worker health check** — upgrade waits for `docker inspect` to report `healthy` instead of blindly running `docker exec`, with 5-minute timeout and clear error messaging

---

## 🐛 Bug Fixes

| Area | Fix |
|------|-----|
| **SMTP** | Postfix container crash on restart due to stale PID file |
| **SMTP** | Google Workspace relay rejected mail — wrong default host |
| **Dify** | Email plugin unusable — SMTP relay had no auth mechanism |
| **Dify** | Webhook triggers returned 401 — routed through Authentik |
| **Upgrade** | `read_env_value` crashed with pipefail on missing VERSION |
| **Upgrade** | CalVer parser rejected `YYYY-MM.GA` format tags |
| **Upgrade** | Authentik-worker health check was unreliable (`docker exec` before ready) |
| **Upgrade** | Blueprint sync silently overwrote manual Authentik UI customizations |

---

## 📊 By the Numbers

| Metric | Value |
|--------|-------|
| Commits since 2026-03.GA | 5 |
| Files changed | 8 |
| Lines added | 316 |
| Lines removed | 123 |
| New env variables | 1 (`SMTP_ALLOWED_SENDER_DOMAINS`) |
| Migrations introduced | 1 (proxy provider token validity) |

---

## ⬆️ Upgrade Path

```bash
# From 2026-03.GA (online)
./razzfazz-upgrade.sh --target 2026.03-GA.P1

# Dry run first
./razzfazz-upgrade.sh --target 2026.03-GA.P1 --check
```

The upgrade automatically:
1. Migrates SMTP relay host default in `.env`
2. Rebuilds the SMTP relay and Caddy containers
3. Runs the `access_token_validity` data migration on Authentik
4. Verifies service health

No manual steps required.

---

Built with ❤️ by the razzfazz.ai team.

---

*Full diff: `git diff 2026-03.GA..2026.03-GA.P1`*
