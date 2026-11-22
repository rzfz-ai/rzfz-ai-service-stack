# What's new — 2026.11

*2026.11 is the cycle in which the box stopped trusting its own parts by default,
and in which knowledge and tools started to follow the person who uses them.
What needs your attention before you upgrade comes first.*

## ⚠️ Before you upgrade

- **On 2026.08 and 2026.09, take a verified backup by hand first.** The backup an
  older box takes at the start of an upgrade can be incomplete and still report
  success. One script checks it and ends with `VERIFIED COMPLETE`; the release
  notes show the three commands.
- **Several services migrate their data one way:** paperless-ngx 3, Onyx 4.9,
  Gitea 28, Stirling-PDF 3, Synapse, Vaultwarden, Cognee and, with observability
  on, OpenLIT 2 with ClickHouse 26.3. Going back means restoring the backup.
- **OpenHands is removed.** Its data volumes are kept; use opencode, Codex or the
  User Defined Agent for coding work.
- **Dify workflows can no longer call the box's own addresses.** Use the box's
  models through Dify's model picker, and list any other ranges you need in
  `DIFY_SSRF_ALLOW_RANGES`.
- **Customer boxes upgrade from the public release channel.** A box still set to
  the internal channel sets `RAZZFAZZ_CHANNEL=public` before its online upgrade.
- **After the upgrade, recreate each personal agent once** with Update or
  Reinstall (stop and start is not enough), so it trusts the box's own
  certificate authority.

→ Read **Before you upgrade** and **Upgrade instructions** in the
[release notes](RELEASE_NOTES.md) before you start.

---

## 🔒 Least privilege by default

Each part of the box now holds only what it needs.

- Application services receive only the settings they read, instead of the
  whole stack configuration, so a compromised application container no longer
  holds every secret.
- No personal or coding agent holds the host's Docker socket.
- The local model engines run as a normal user, on a read-only file system,
  with their model files mounted read-only, and the upgrade moves running
  engines onto these settings.
- Passwords and tokens stay out of logs and, with one named exception, off
  command lines, and containers on the stack host can no longer reach a cloud
  server's metadata address.
- A fresh installation hardens its host before the stack starts.

## 👥 Knowledge follows the person

Who may read which knowledge is now decided once, not separately in every app.

- **Knowledge scopes** decide who reads which data in Chat, Dify and Cognee; the
  Settings portal shows who can read each scope.
- **Removing someone from a scope takes effect within the hour** in Dify and
  Cognee, and at their next sign-in in Chat.
- **Microsoft and Google sign-in** bring the person's group memberships into the
  box at every sign-in once the groups are mapped, and an unverified e-mail address can no longer take over
  an existing account.
- `rzfz status` warns when someone outside a scope can still read its data.

## 🧩 Agents and tools act as the signed-in person

- **The MCP & Agent Manager is a production module**; its Jira & Confluence and
  Microsoft 365 connectors are experimental.
- **One MCP gateway** connects Jira & Confluence, Microsoft 365 (read-only),
  Gitea and Docling to every agent and to Chat, each through the person's own
  connection; Dify workflows use a technical user.
- **Technical users** give an automation or an autonomous agent its own
  restricted identity, with a token shown once.
- **OpenBao** (experimental) keeps secrets and personal tool connections;
  Infisical is deprecated.

## 🧠 An LLM fleet that says what it does

- The **LLM Manager** explains and lets you choose where a model runs, refuses a
  model that cannot fit with a reason, and notices a lost Linux worker within
  seconds.
- A briefly unavailable model answers `503` with `Retry-After`, so applications
  retry, and every request has a ceiling on generated tokens.
- `https://llm.<your-domain>/v1/responses` serves the **OpenAI Responses API**.
- **llama.cpp v0.9.0**, a stable release, runs the local engines on AMD,
  NVIDIA GB10 and CPU workers, and the upgrade moves running engines onto it.
- **A Mac can join the fleet as a native worker**, with its own managed Ollama.

## 🛟 Upgrades, backups and restores stop instead of pretending

- From 2026.11 on, a failed pre-upgrade backup stops the upgrade, and an
  upgrade ends with a health check before it records the new version.
- A restore names every volume it could not bring back.
- Rotating the backup password keeps earlier archives restorable.
- The nightly backup waits for a running upgrade, and up to two hours for a
  running knowledge import, and an incomplete archive is never marked complete.

## 📴 Offline boxes stay quiet

- Search, sign-on, documents and the help mirror make no outbound lookups on a
  box set to offline, and one script quiets the host's own background lookups.
- Speech models, Dify indexing and the time source keep working without the
  internet.

## ⬆️ The larger upstream steps

| Component | Now |
|---|---|
| Open WebUI | 0.12 |
| Dify | 1.18 |
| Cognee | 1.6 |
| Authentik | 2026.8 |
| Gitea | 28 |
| paperless-ngx | 3 |
| Stirling-PDF | 3 |
| OpenLIT · ClickHouse | 2 · 26.3 |
| Onyx | 4.9 |
| llama.cpp | v0.9.0 |

Upgrading from 2026.09-ga.5 is a supported one-step upgrade with 153 environment
migrations. Every module, defect fix and known issue is in the
[release notes](RELEASE_NOTES.md) and in the Help Center under Known issues.
