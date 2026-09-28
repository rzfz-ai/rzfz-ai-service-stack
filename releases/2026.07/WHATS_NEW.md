# What's New — 2026.07 cycle

The 2026.07 cycle is about **opening the box up**: a published open-core licence,
a public place to get it, one command to run it, and a personal AI-agent workspace
for every user. It is also the cycle where the product becomes **the rzfz.ai Stack**.

## Steadier all round *(ga.10)*

- 🔧 **Installs and upgrades are more robust** — post-install no longer stalls on CPU-only
  boxes, and `rzfz upgrade` now initialises Dify automatically.
- 💾 **Quieter backups** — the nightly archive no longer briefly pauses the database, so
  services stay responsive during the 03:00 backup.
- 🔗 **Published Dify apps just work** — reachable by their share link without an SSO login.
- 🔎 **Onyx AI-search chat** answers with citations out of the box, using your local model.
- 🔐 **Security update** — the identity provider is updated to close upstream advisories.
- 📚 Refreshed documentation, including a new customs-classification (TARIC) tutorial.

## Day one, actually ready *(ga.1)*

- ✅ **Chat-ready on day one.** After setup, Open WebUI lists the deployed models and
  the default chat model answers straight away — no empty model list, no blank reply.
- 🧩 **Every module turns on cleanly**, instantly and even on locked-down or offline
  networks — no on-the-fly image build that could fail.
- ⬆️ **Upgrades land working too** — `rzfz upgrade` now applies the day-1 setup fixes
  and deploys any missing default models automatically.
- 🧪 Backed by a new **Day-1 user-journey test tier** that deploys a model, logs in,
  chats and clicks each module UI on a live box before every release.

## Open source, out in the open *(rc1 → ga)*

- 📜 **A real licence, published.** A free Apache-2.0 **Community** tier, a
  source-available **Business Source License 1.1** tier governed by the **rzfz.ai
  Subscription** (private and evaluation use stay free — no key, no registration),
  and bundled upstreams under their own licences. A live overview is at
  `license.<domain>`.
- 🌐 **Get it from Codeberg.** Customer boxes pull from a public Codeberg mirror
  (`RAZZFAZZ_CHANNEL=public`), with a published Community Wiki and a hardened export
  that ships only what a customer needs.
- 🏢 **Enterprise docs travel with the box.** Entitled boxes carry a gated
  documentation overlay that the Help Center mounts at runtime — delivered by the
  USB build and `rzfz package`, never published to the public mirror.

## One command, one product name *(rc1)*

- ⌨️ **`rzfz` runs everything** — `rzfz init`, `rzfz upgrade`, `rzfz setup`,
  `rzfz backup`, `rzfz post-install`. The old `setup.<domain>` web wizard is gone;
  first-run is `rzfz init` on the host, RAG-model and TLS-certificate management
  move into the Configuration Portal.
- 🎨 **Rebranded to rzfz.ai** across the UI, CLI and documentation.

## A personal AI-agent workspace for every user *(rc1)*

- 🧑‍💻 **Sandboxed coding agents** — opencode, Codex and a user-defined slot, each
  isolated, with a first-party Hermes dashboard and a live preview of the web app
  your agent is building.
- 🧠 **Your own MCP + memory** — per-user Model-Context-Protocol proxies with an
  encrypted credential vault, and a two-tier "memory" (a private brain plus a shared
  company brain) wired straight into the agents, all under one **MCP & Agent Manager**.
- 🔑 **Change your password once** and it fans out to Authentik, Dify and Cognee.

## Smoother day-to-day *(rc1)*

- 🔀 **Dify 1.15.0**, built from a clean upstream clone.
- ✅ **One sign-in, trusted end-to-end** — group-to-role mapping for Open WebUI,
  Gitea, Dify and Cognee, and no more double-login prompt.
- 🔒 **Version-currency + CVE sweep** across Open WebUI, Gitea, SearXNG, Crawl4AI,
  ClickHouse, LightRAG and Cognee, plus the legacy GPUStack Vulkan base rebuilt.

_General Availability: v2026.07-ga (2026-07-06); latest patch v2026.07-ga.9
(2026-07-11). See the full release notes via the Release Notes link in the nav._
