# Release Notes — 2026.09

**Cycle window:** 2026-08-15 → 2026-09-17 · **GA tag:** `2026.09-ga` (cut from `2026.09-rc11`, the candidate all four box journeys passed on)
**Previous GA:** `v2026.08-ga.15`

---

## Executive summary

2026.09 is the cycle in which the box stopped having several LLM front doors and
gained one. The **LLM Manager** is now the inference front end of every box: a
single OpenAI-compatible endpoint at `https://llm.<domain>/v1` that answers
whichever backend actually holds the model, with its own keys, its own metering
and its own console. GPUStack did not disappear — it became one backend behind
that endpoint instead of the product itself. This is the change customers will
notice first, because it is also the cycle's one breaking change: the URL keeps
working, the old API key does not.

Alongside it the box gained two new security-facing modules and a serious
upgrade to how it proves itself. **Wazuh** brings file-integrity monitoring, log
correlation and compliance mappings; **OpenUEM** brings fleet inventory, software
deployment and remote assistance, each with its own PKI and its own Authentik
gate. Both ship EXPERIMENTAL and off by default. Less visible but larger in
effect: the **day-1 acceptance tier** now rotates every module through a real
browser and asserts that it *does* something, rather than that its container is
healthy — and a batch of checks that had been written but never actually ran in
any gate were found and wired in.

Upgrading is a big-bang step from `2026.08-ga.15`, carrying **242 environment
migrations**, five new profiles, seventeen new images and **nineteen moved image
pins** — Authentik, Dify and its plugin daemon, Open WebUI (0.10.2 → 0.11.3),
Komodo, Gitea, LightRAG, Gotenberg, Crawl4AI, Valkey, Docling, Vaultwarden,
Stirling-PDF, Element Web, cognee-mcp and the agent images; each module section
below names its old and new version and what changed upstream. None of the bumps
needs a manual `.env` step. The audit posture for the GA tag is established by
the release security review against exactly this pin set; the findings section
below is filled from that run.

## Highlights

What a customer sees first, in the order it changes their day. Each item has its
detail further down.

- **The LLM Manager and its Console** (`https://llm.<domain>`) replace GPUStack as the LLM front end of every box: deployments from a catalog or the Hugging Face browser, a fleet page for workers and nodes, the model hub, API keys with per-key limits and token metering, a playground for chat, embeddings and rerank, a live dashboard whose curves follow the measurements, target-worker selection when scaling, and multi-token prediction on the models that support it.
- **One OpenAI-compatible endpoint** — `https://llm.<domain>/v1` with `rzfz-sk-…` keys; the old URL keeps working, the old GPUStack key does not (the cycle's one breaking change).
- **The Agent Manager became the fourth app of the one-identity family:** a per-user agent portal that shows instances rather than types (several agents of one kind per user), opens on the agent's own web UI, lets users name their agents, gives administrators start/stop/memory control over every user's agents with an audit trail, and fences the agent classes behind an optional default-deny egress proxy; personal MCP integrations sit in the same portal.
- **Start portal redesign** with favourites and categories on the rail, tiles for every new module, and a rail that finally scrolls what it points at.
- **Shortcuts:** curated one-click tiles on the Start portal — a Chat persona with a preset model and prompt, a Dify chat app, or an external assistant opened with a prefilled prompt — defined once in the shortcuts editor, scoped to Authentik groups, with a Shortcut Authors group that publishes company-wide and curated or uploaded icons per tile.
- **Settings portal redesign** — the configuration portal moved to `settings.<domain>`, got the information-first layout of the LLM Console, a master-detail settings pane, module toggles for the five new profiles, GPUStack-enable repairs and the agents' global memory limits.
- **Help Center redesign** — two-level navigation, local full-text search, upstream documentation mirrored for every module (single-file captures where a site cannot be crawled), box-local pages where a mirror cannot exist, and own guides for the LLM Manager and the Agent Manager.
- **Licenses & Attribution redesign** — tier cards, licence-family chips, an in-site side pane for the vendored licence texts; the texts ship in the tree instead of being fetched from GitHub at build time (which throttled upgrades).
- **One look across all apps:** the razzfazz signature background and the dark navigation rail are the same in the Start portal, Settings, Licenses, Help Center, LLM Console and Agent Manager.
- **Wazuh** (EXPERIMENTAL): SIEM/XDR with file-integrity monitoring, log correlation and compliance mappings, alert mail to the operator.
- **OpenUEM** (EXPERIMENTAL): endpoint inventory, software deployment and remote assistance for the fleet.
- **Chat (Open WebUI 0.11):** redesigned interface, human-in-the-loop tool approval, models that can ask you a multiple-choice question, sub-agents (admin-enabled), document previews, rebuilt streaming and two major security batches.
- **Workflows (Dify 1.17):** dataset-scoped Knowledge API keys (role-based RAG access), human-input-to-workflow callbacks, Skill management, an agent sandbox, and the private-network request policy applied to agent skills.
- **Day-1 acceptance grades behaviour, not container health**, and a batch of checks that ran nowhere now run on every pull request.
- **Security bumps across the stack:** Authentik 2026.5.7, Gitea 1.27.3, Valkey 9.1.2, Crawl4AI 0.9.3, Element Web 1.12.27, Vaultwarden 1.37.2 (required for Bitwarden clients ≥ 2026.8.0) — see each module's section for what changed upstream.

---

## Table of contents

- [Highlights](#highlights)
- [Breaking changes](#breaking-changes)
- [Cross-cutting changes](#cross-cutting-changes)
  - [One LLM front door](#one-llm-front-door)
  - [Wazuh: file integrity, log correlation, compliance](#wazuh-file-integrity-log-correlation-compliance)
  - [OpenUEM: endpoint management](#openuem-endpoint-management)
  - [Day-1 acceptance: health is not function](#day-1-acceptance-health-is-not-function)
  - [Observability that says what it sends](#observability-that-says-what-it-sends)
  - [Checks that ran nowhere](#checks-that-ran-nowhere)
- [Module-by-module changes](#module-by-module-changes)
- [New and reworked operator surfaces](#new-and-reworked-operator-surfaces)
- [Upgrade instructions](#upgrade-instructions)
- [Security](#security)
- [Known issues at GA](#known-issues-at-ga)

---

## Breaking changes

### The OpenAI-compatible API needs a new key

**Who is affected:** every box, and every consumer of its OpenAI-compatible API —
scripts, editor agents (VS Code and friends), CI jobs, and anything else holding
an API key. Boxes used only through the chat interface are not affected.

**What changed.** Until now `llm.<domain>` was GPUStack and the customer API lived
at `https://llm.<domain>/v1-openai`, authenticated with `GPUSTACK_API_KEY`. From
2026.09 the **LLM Manager** is the front of every box: one canonical endpoint,
whichever backend actually runs the model.

| | Before | After |
|---|---|---|
| Base URL | `https://llm.<domain>/v1-openai` | `https://llm.<domain>/v1` |
| Old URL | — | keeps working, served as an alias |
| API key | `GPUSTACK_API_KEY` | an `rzfz-sk-…` key from the manager console |
| Old key | — | **refused: `401 {"error": {"message": "invalid API key", …}}`** |

**The URL keeps working; the key does not.** That is the whole of the break, and
it is deliberate rather than an oversight: the manager key is what carries
metering, cost centres and per-consumer revocation. A request arriving with a
backend's own credential identifies nobody and cannot be billed, reported or
revoked selectively.

**What customers must do,** after their box is upgraded (the new key does not
exist before then): mint a key per consumer in the manager console at
`https://llm.<domain>/` → **Keys**, and swap it into each client. Roughly five
minutes per client.

→ **Step-by-step:** open the Help Center on your box at `https://help.<your-domain>/`
and read **Guides → Migrate your OpenAI-compatible API access**
(`migrate-openai-api-2026.09`).

**What operators must do:** tell the people who hold keys **before** upgrading.
They cannot mint a replacement until the box is on 2026.09, so an unannounced
upgrade means every API consumer on that box fails at once with a 401.

**Bridge for anyone who needs longer.** Where the GPUStack backend is still
installed it keeps its own endpoint and its own key —
`https://gpustack.<domain>/v1-openai` with the unchanged `GPUSTACK_API_KEY`. It
talks to one backend directly and appears in no usage report, so it is a
transition, not a destination.

---

## Cross-cutting changes

### One LLM front door

The largest change of the cycle, and the one every other LLM change hangs off.

Before, a box could be running GPUStack 2.x, GPUStack 0.7.1, a CPU-only variant
or a Mac gateway, and each consumer had to know which. Now the **LLM Manager**
owns the canonical endpoint and the backends register behind it.

- `https://llm.<domain>/v1` is the one OpenAI-compatible surface for chat,
  embeddings and rerank, and every in-stack consumer is wired to it unconditionally.
- `/v1-openai` remains as an alias so existing URLs keep resolving.
- GPUStack 2.x and the separate CPU profile were removed from the product; the
  remaining GPUStack 0.7.1 is one optional backend selected by `HARDWARE` plus a
  device overlay, and it now runs **non-root** by default.
- vLLM was removed as a runtime: GGUF is the fleet's single model architecture,
  which makes a model artifact portable between every node the fleet runs.
- The manager chooses quantisation itself and computes a deployment's memory
  footprint before placing it, so a deployment that can never be placed says why
  instead of waiting.
- A runner switch now starts the new engine **before** tearing the old one down
  where the weights fit twice, and says in advance whether it will interrupt
  serving — including whether that verdict could be computed at all.
- A new **model registry** (Zot) holds model artifacts as content-addressed blobs
  so fleet nodes pull by digest, deduplicated.
- A new **worker agent** launches and supervises the engine containers the
  manager deploys and self-registers what it serves, driving Docker only through
  a scoped socket proxy.
- Thin inference nodes are a declared role rather than an accident of which files
  happen to be present, and they file a node report of their own.

### Wazuh: file integrity, log correlation, compliance

A new SIEM/XDR module: manager, indexer and dashboard, with its own certificate
authority and its own Authentik gate.

- File-integrity monitoring, log correlation and compliance mappings, behind the
  `wazuh` profile and off by default.
- A host agent covers the gap the manager's own 12-hour scan cadence leaves: a
  change to a watched host file alerts in seconds rather than up to half a day.
- Agent enrolment is password-gated; auto-enrolment ships **off** and stays off
  until the release security assessment clears it.
- Remote command execution from manager to agent is disabled by default, and
  `nodiff` exemptions keep `.env` contents out of the search index while still
  reporting that the file changed.
- The module's volumes are in the backup allow-list and the restore targets.

### OpenUEM: endpoint management

Unified endpoint management — fleet inventory, software deployment and remote
assistance — as a second EXPERIMENTAL security-facing module.

- Six services plus a NATS broker and its own certificate authority.
- Its own database and role, created at install; secrets generated on install and
  on upgrade.
- Behind an Authentik forward-auth gate with its own group and bindings; only the
  console is allow-listed for egress.
- LAN-facing endpoints bind through an explicit host-bind variable rather than
  being exposed by default.
- A super-admin is seeded with the stack password so an operator can log in
  immediately after enabling it.

### Day-1 acceptance: health is not function

The acceptance tier stopped grading containers and started grading behaviour.

- Every module with a UI is driven through a real browser, logged in through
  Authentik SSO, and made to **do one thing** whose outcome is asserted.
- Modules without a UI need a functional probe too, or a recorded reason why they
  cannot have one — and the reason expires when a probe arrives.
- A module whose probes were **all skipped** is no longer reported as a pass: a
  run that checked nothing is not a run that found nothing.
- The rotation enables and disables each module in turn, unattended, so a module
  that only works because it was already running is caught.
- Probe specifications carry their provenance — measured on a box, or inferred —
  so a plausible guess can never be mistaken for a measurement.

### Observability that says what it sends

The observability wiring was present but silent about its own configuration,
which made every measurement ambiguous.

- Dify and the personal agents now **state** their OTEL transport and sampling
  rate instead of inheriting them, so a span that never arrives is a defect
  rather than a guess about which default applied.
- An upgrade corrects stale OTEL values left behind by earlier cycles instead of
  keeping an operator's outdated choice.
- Exporters for PostgreSQL and Valkey join the observability profile.
- The observability module's own outbound product analytics is disabled by
  default, because a self-hosted sovereignty surface should not phone home.

### Checks that ran nowhere

A quieter theme, and the reason several defects in this cycle were found at all:
a gate that does not run is worse than a red one, because nothing looks wrong.

- Around forty static shell suites lived outside every gate; the ones that carry
  real assertions were lifted into the tier that actually runs on a pull request.
  building images at runtime — had never fired on a pull request.
- The SSO/OIDC suite could report success having rendered nothing, because a
  missing tool and a passing check were indistinguishable; it now refuses to
  start without its renderer.
- A ratchet was added so a new compose environment key cannot ship without
  documentation.
- The scripts tier now gates, and the inode guard measures the shipped tree
  rather than the working copy.
- The install suite's profile-to-container map knew none of the four LLM profiles
  2026.09 ships, so it expected **zero** containers for a default box's inference
  stack and would have passed a box that started none of it; the map is now read
  from the compose files and a guard derived from the default profile line fails
  the moment a shipped profile has no named containers.
- A run that performed no test no longer scores as a pass: the init suite refuses
  to prompt without a terminal and exits 2, and a declined confirmation is not a
  verdict.
- The pre-merge gate's PARTIAL verdict names every tier it did not verify and
  says whether a tier could not run here or was simply not asked to.

---

## Module-by-module changes

### LLM Inference

#### LLM Manager

| Version | Profile | License |
|---|---|---|
| New in 2026.09 | `llm-manager` | Proprietary |

**What's new in 2026.09**

- The canonical OpenAI-compatible endpoint for chat, embeddings and rerank, served at `https://llm.<domain>/v1`.
- A console at `https://llm.<domain>/` for deployments, workers, keys and the playground.
- Per-consumer API keys carrying metering, cost centres and selective revocation.
- The manager picks a model's quantisation and computes its memory footprint before placing it.
- A deployment that can never be placed reports the reason instead of waiting silently.
- A runner switch starts the replacement engine before retiring the old one when the weights fit twice, and reports in advance whether it will interrupt serving.
- A stalled transfer ends rather than hanging, and a command nobody completes expires.
- Scale-up can name the target worker.
- The routing rules are the completion criterion for a switch, so `done` means the fleet serves rather than the node being content.

**Security**

- Backend credentials no longer reach customer-facing requests; the manager key is the only accepted credential on the canonical endpoint.

**Migration**

- Consumers must mint a new `rzfz-sk-…` key after the upgrade; see [Breaking changes](#breaking-changes).

---

#### LLM Worker-Agent

| Version | Profile | License |
|---|---|---|
| New in 2026.09 | `llm-worker-agent` | Proprietary |

**What's new in 2026.09**

- Launches and supervises the engine containers the manager deploys, on the master node and on remote workers alike.
- Self-registers what it serves, so the manager's inventory reflects reality rather than intent.
- Drives Docker through a scoped socket proxy and never the raw socket.
- Re-adopts engines it already runs after a restart, so a node restart does not orphan them.
- Reports the node's stack version and the engine build behind it.

---

#### LLM Model Registry (Zot)

| Version | Profile | License |
|---|---|---|
| New in 2026.09 | `llm-registry` | Apache-2.0 |

**What's new in 2026.09**

- A content-addressed OCI registry holding model artifacts as blobs.
- Fleet nodes pull by digest into their models volume, deduplicated across nodes.

---

#### <img src="/branding/media/razzfazz-ai_llm_icon.png" width="20" height="20"> LLM Inference (GPUStack 0.7.1)

| Version | Profile | License |
|---|---|---|
| Carried forward, now a backend | `llm-legacy` | Apache-2.0 |

**What's new in 2026.09**

- GPUStack is now an optional backend registered behind the LLM Manager rather than the box's LLM front end.
- GPUStack 2.x and the separate CPU-only profile were removed; one 0.7.1 service remains, selected by `HARDWARE` plus a device overlay.
- The service runs **non-root** by default on AMD/Vulkan boxes, with a one-time volume ownership migration on upgrade.
- vLLM was removed as a runtime; GGUF is the fleet's single model architecture.

**Migration**

- A box arriving with a retired `llm` or `llm-cpu` profile token is migrated automatically, and `rzfz status` fails loudly on a leftover token rather than starting nothing.

---

#### Mac LLM Gateway (LiteLLM)

| Version | Profile | License |
|---|---|---|
| Carried forward | `mac-llm` | MIT |

**What's new in 2026.09**

- Registered behind the canonical endpoint like every other backend.

---

### Security

#### <img src="/branding/media/razzfazz-ai_wazuh_icon.png" width="20" height="20"> Wazuh

| Version | Profile | License |
|---|---|---|
| New in 2026.09 | `wazuh` | GPL-2.0 AND Apache-2.0 |

**What's new in 2026.09**

- SIEM/XDR with file-integrity monitoring, log correlation and compliance mappings, EXPERIMENTAL and off by default.
- A host agent whose file-integrity monitoring is realtime, against the manager's own 12-hour scan cadence.
- Container log shipping restricted to an explicit allow-list of container names.
- An off-by-default security-plugin debug logger for diagnosing dashboard authentication.
- Optional cloud-security log ingestion that activates only when credentials are present and keeps no secret in the config file.
- Roughly 5 GB of RAM and `vm.max_map_count>=262144`; enrolment is password-gated and LAN-facing through an explicit bind variable.
- Alert mail is on: alerts at or above level 12 reach `RAZZFAZZ_OPERATOR_EMAIL` (seeded from the install's admin address) through the stack relay; a box without a recipient says so instead of staying silent (#2002).
- On Ubuntu 26.04 hosts the rootcheck trojan signatures for ten coreutils names are narrowed — the Rust multi-call coreutils binary contains the string `bash` the stock signatures look for — in a derived file the manager's shared-config sync cannot overwrite (#1983).

**Security**

- The certificate tool is fetched and checksum-verified at image build time, so PKI generation needs no internet access at container start.
- Manager-to-agent remote command execution is disabled by default.
- `nodiff` exemptions keep `.env`, `.env.dify` and certificate contents out of the search index while still reporting that they changed.
- Agent auto-enrolment ships off and remains off until the release security assessment clears the enlarged enrolment surface.

---

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> OpenUEM

| Version | Profile | License |
|---|---|---|
| New in 2026.09 | `openuem` | Apache-2.0 |

**What's new in 2026.09**

- Fleet inventory, software deployment and remote assistance, EXPERIMENTAL and off by default.
- Six services plus a NATS broker, its own certificate authority and its own database.
- A super-admin is seeded with the stack password so the console is usable immediately after enabling.
- A post-install readiness report covers PKI, console reachability and enrolment material.
- The console's mail settings inherit the stack relay at first start (`smtp-relay:587`, `LOGIN`, sender `SMTP_FROM`); the relay presents a certificate the stack CA signs, so the console's mandatory TLS verification passes (#1992, #2004, #2027).

**Security**

- Behind an Authentik forward-auth gate with its own group and bindings.
- Only the console is allow-listed for egress; every other service is never-listed.
- LAN-facing NATS binds through an explicit host-bind variable.

---

#### <img src="/branding/media/razzfazz-ai_vault_icon.png" width="20" height="20"> Vaultwarden

| Version | Profile | License |
|---|---|---|
| `1.37.1 → 1.37.2` | `vaultwarden` | AGPL-3.0 |

**What's new in 2026.09**

- Declared stable for 2026.09 with Authentik OIDC single sign-on.
- Upstream 1.37.2 is required for Bitwarden clients 2026.8.0 and later; no configuration change.

---

#### <img src="/branding/media/razzfazz-ai_secrets_icon.png" width="20" height="20"> Infisical

| Version | Profile | License |
|---|---|---|
| Carried forward | `infisical` | MIT |

**What's new in 2026.09**

- Outbound product analytics disabled in the shipped configuration.

---

### Monitoring

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Observability

| Version | Profile | License |
|---|---|---|
| Carried forward | `observability` | Apache-2.0 |

**What's new in 2026.09**

- Dify and the personal agents state their OTEL transport and sampling rate instead of inheriting them.
- Exporters for PostgreSQL and Valkey were added to the profile.
- An upgrade corrects stale OTEL values from earlier cycles rather than preserving an outdated choice.
- The Portal toggle and `rzfz post-install` now wire the same four consumers (Open WebUI, Dify, the personal agents, the LLM Manager and its router), and the toggle recreates the containers it re-points instead of leaving a note (#2015).

**Security**

- The module's own outbound product analytics is disabled by default.

---

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Monitoring (Komodo)

| Version | Profile | License |
|---|---|---|
| `2.2.0 → 2.3.3` | `monitor` | GPL-3.0 |

**What's new in 2026.09**

- Upstream 2.3: paginated resource lists with server-side sorting, memory split into used and cache (ZFS-aware), cancellation of running builds and procedures, a multi-server stats page, distributed builds across attached servers.
- Upstream 2.3.x fixes: registry accounts restored from the database, the compose editor's suggestions back, the intermittent "Project Missing" report on stack timeouts gone.
- The periphery agent moves in lockstep with the core (one `KOMODO_VERSION`), and the manifest records both (#1996).

---

### Agents

#### MCP & Agent Manager

| Version | Profile | License |
|---|---|---|
| Carried forward | `agents` | Proprietary |

**What's new in 2026.09**

- A portal shell with an agent tree, status circles and one live pane.
- A same-origin agent proxy at `/i/<token>/` with an owner gate.
- One-click reconnect, repair and restart, with per-instance routes healing the moment the proxy returns.
- Per-instance naming and an editable process cap.
- A default-deny egress proxy with an internal fence overlay for the agent classes.
- An admin can start, stop and resize any user's agent; the action is audited under the admin's identity, and the other lifecycle routes stay owner-only (#1957).
- Several instances of one agent type per user are real: the tier's `max_per_type` is enforced, the tree offers another instance while there is room, and existing instances arrive as #1 with their names untouched (#1988).

**Security**

- The pane may frame its own agent without the upstream's framing policy blocking it, and the direct route keeps the upstream policy unchanged.
- Repeated response headers survive the proxy, including redirect rewriting into the pane.
- A launch control that cannot act says why instead of failing silently.

---

---

#### Coding agents (OpenCode, Codex) and the unchanged agent apps

| Version | Profile | License |
|---|---|---|
| OpenCode `1.18.4 → 1.18.25`, Codex `0.144.6 → 0.151.0`; OpenHands `1.6.0` and Paperclip `v2026.707.0` unchanged | `agents` / `openhands` / `paperclip` | MIT / Apache-2.0 |

**What's new in 2026.09**

- The OpenCode CLI moves to 1.18.25 in the coding-tools and per-user coding-agent images.
- The Codex CLI moves to 0.151.0 in the per-user coding-agent image.
- OpenHands and Paperclip keep their 2026.08 versions; no upstream move was taken this cycle.

### AI & Chat

#### <img src="/branding/media/razzfazz-ai_chat_icon.png" width="20" height="20"> Chat (Open WebUI)

| Version | Profile | License |
|---|---|---|
| `0.10.2 → 0.11.3` | `chat` | MIT |

**What's new in 2026.09**

- Wired unconditionally to the canonical LLM endpoint rather than resolving a backend by container name.
- Upstream 0.11.0 reorganised the interface: narrower conversation columns, consistent menus and reorganised settings across the chat view and the admin panel.
- Upstream 0.11.1 rebuilt streaming (up to 1000× less data on long replies), added human-in-the-loop tool approval and models that ask multiple-choice questions, a terminal file browser, and admin-configurable task-model parameters.
- Upstream 0.11.2 added document previews with page thumbnails, a `request` filter step and an interface font setting; the accessibility mode replaces the former high-contrast mode.
- OAuth and OIDC have independent switches since 0.11.0 and the stack sets the master toggle `ENABLE_OAUTH` explicitly, so single sign-on stays on across the upgrade.
- The first start on 0.11.x runs a PostgreSQL chat-search backfill that scales with chat history; a big-history box boots longer once, it does not hang.

**Migration**

- The 0.10 → 0.11 native-OIDC login failure is repaired by the upgrade's OAuth configuration reconcile; no manual step.

**Security**

- Upstream 0.11.0 shipped a security advisory (terminal HTML preview isolated, math expressions rendered as text, upload-parser library update, deactivated accounts lose live access, chat-ownership checks on completion requests).
- Upstream 0.11.1 closed access-control gaps: knowledge search respects collection access, archive expansion is bounded, code runs only through explicit tool calls, a password change revokes other sessions, workspace models cannot shadow provider models.
- Upstream 0.11.2 carries further security fixes whose advisories were withheld at release time.

---

### Workflows

#### <img src="/branding/media/razzfazz-ai_workflow_icon.png" width="20" height="20"> Workflows (Dify)

| Version | Profile | License |
|---|---|---|
| `1.16.0 → 1.17.1` | `dify` | Apache-2.0 |

**What's new in 2026.09**

- Wired unconditionally to the canonical LLM endpoint.
- States its OTEL transport and sampling rate explicitly.
- Upstream 1.17.0 added the Agent sandbox, skill management and workspace-level skills, and a human-input-to-workflow callback.
- Upstream 1.17.1 added dataset-scoped knowledge-base API keys, keyboard movement of workflow nodes and a marketplace redesign.
- The document extractor gains the `unstructured` extras for `.msg` attachments in a stack-built API image, measured for size before it ships (#1981).
- Model plugins stop auto-upgrading to `latest`: the pinned plugin versions are the versions in service (#1972).

Sub-images bumped together with the main image:

| Sub-image | Version |
|---|---|
| dify-plugin-daemon | `0.6.4-local → 0.6.10-local` — concurrent plugin installs no longer collide, per-field credential help, gRPC security update |
| dify-sandbox | `0.2.15`, unchanged |

**Migration**

- 1.17.1 adds three database migrations, one of them not reversible; the upgrade's backup runs before them.
- Upstream's default agent-run retention drops from three days to two hours (`DIFY_AGENT_RUN_RETENTION_SECONDS`); set it in `.env.dify` if you need longer.
- Upstream recommends re-importing CSV, Excel, Notion and web-scraped documents after 1.17.1 for the extraction fixes to apply to existing knowledge.
- The bundled Weaviate upgrade upstream describes does not apply: the stack's Dify vector store is pgvector.

**Security**

- Upstream 1.17.1: agent skills respect the private-network request policy — the same boundary the stack's `DIFY_DOC_TOOLS_SSRF_ALLOW` relies on — and the blob chunk merger validates sizes before allocating.

**Migration**

- An upgrade rewrites stale Dify OTEL values that earlier cycles left behind.

---

### Knowledge

#### <img src="/branding/media/razzfazz-ai_cognee_icon.png" width="20" height="20"> Cognee (GraphRAG)

| Version | Profile | License |
|---|---|---|
| `cognee-mcp 1.4.0 → 1.5.3.dev1` | `cognee` | Apache-2.0 |

**What's new in 2026.09**

- Wired to the canonical LLM endpoint.
- Upstream 1.5: hybrid keyword+vector search by default, a dataset overview index, resumable ingestion with progress, WebSocket brain and dataset summaries, Linear and GitHub App integrations.
- The MCP image ships as a vendor pre-release (`1.5.3.dev1`): Docker Hub carries no `1.5.3` tag for `cognee-mcp` although the backend release exists — a conscious decision, recorded in the known issues.

**Security**

- Upstream: API tokens are redacted from startup logs, WebSocket run subscriptions require authentication, local file-root access is disabled by default; none of these change the stack's configuration (graph provider `kuzu`, no local-file ingestion).

---

### Documents

#### <img src="/branding/media/razzfazz-ai_docling_icon.png" width="20" height="20"> Docling

| Version | Profile | License |
|---|---|---|
| `docling-serve v1.27.0 → v1.32.0` | `docling` | MIT |

**What's new in 2026.09**

- Remains Open WebUI's document extractor when the profile is enabled.
- Upstream: PDF heading-level inference, new chunking options and multiple targets, plugin-defined connectors, presigned artifact storage; invalid multipart options answer 422 instead of 500.
- A new optional `DOCLING_SERVE_ENG_RQ_JOB_TIMEOUT` bounds long conversions; the stack keeps the upstream default.

---

#### <img src="/branding/media/razzfazz-ai_pdf_icon.png" width="20" height="20"> Stirling-PDF

| Version | Profile | License |
|---|---|---|
| `2.14.2 → 2.14.3` | `stirling-pdf` | MIT |

**What's new in 2026.09**

- Declared stable for 2026.09.
- Upstream hotfix: merge/split on Debian 12, concurrent use, and 2 GB PDFs no longer crash the service.

---

### Search

#### <img src="/branding/media/razzfazz-ai_search_icon.png" width="20" height="20"> Crawl4AI

| Version | Profile | License |
|---|---|---|
| `0.9.0 → 0.9.3` | `crawl4ai` | Apache-2.0 |

**What's new in 2026.09**

- The acceptance probe now measures the crawl request itself rather than the text the interface paints afterwards.

**Security**

- Upstream 0.9.3 is a coordinated-disclosure security release for the PDF path and the Playground UI: a file-write and an outbound-request weakness in the PDF path, unbounded PDF size, and two cross-site scripting issues.

---

### Core

#### Authentik

| Version | Profile | License |
|---|---|---|
| `2026.5.5 → 2026.5.7` | *(core)* | MIT |

**What's new in 2026.09**

- Upstream fixes: SCIM responses with mixed-case keys, RADIUS with an empty message authenticator, Platform SSO token errors answer 400, brand flag schema, policy request-context pollution, dynamic captcha keys in an embedded identification stage.
- The image no longer ships `curl`; the stack's healthcheck uses authentik's own `ak healthcheck`, so a clean install no longer aborts on a server that is healthy but cannot be probed (#2033).

**Security**

- Upstream security patches in 2026.5.7: group-hierarchy roles, SAML, libxml2 doctype handling, e-mail recipient override, secrets read permission.

**Migration**

- New installs get a one-day default token duration upstream; existing tokens are untouched.

---

#### <img src="/branding/media/razzfazz-ai_git_icon.png" width="20" height="20"> Gitea

| Version | Profile | License |
|---|---|---|
| `1.27.1 → 1.27.3` | `gitea` | MIT |

**What's new in 2026.09**

- Upstream: permalinks to pull-request reviews, npm package metadata, about fifty bug fixes across Actions workflows, package registries, LFS transfers and pull-request merging.
- The admin address on a new install is `razzfazz-ai-admin@<domain>` instead of `@localhost`; an existing admin keeps the address it was created with (#2037).

**Security**

- Upstream 1.27.2 and 1.27.3 each carry a security batch: collaborator access modes, external render, WebAuthn verification, markup, packages, attachments, Actions, API access controls, migrations, git operations.

---

#### Gotenberg

| Version | Profile | License |
|---|---|---|
| `8.34.0 → 8.37.0` | `gotenberg` | MIT |

**What's new in 2026.09**

- Upstream: OIDC bearer authentication, PDF image optimisation, multiple stamps per request, element screenshots, table-of-contents bookmarks, concurrent PDF engines and a bounded `downloadFrom`.
- Upstream 8.35 stopped inheriting `HTTP_PROXY`/`HTTPS_PROXY` implicitly; the stack never handed Gotenberg a proxy, so proxied boxes behave as before.

**Security**

- Upstream: URL userinfo stripped before allow/deny matching, CGNAT ranges treated as non-public, a sanitised output-filename header, bounded header scopes, WebSocket handshakes filtered by the outbound policy.

---

#### Valkey

| Version | Profile | License |
|---|---|---|
| `9.1.1 → 9.1.2` | *(core)* | BSD-3-Clause |

**What's new in 2026.09**

- Upstream fixes: a module timer double-free, RESP3 push corruption during pub/sub, slot migration with I/O threads and TLS, a monotonic-clock freeze on unsynchronised TSC hosts.

**Security**

- Upstream: two use-after-free fixes — RDMA via `CLIENT KILL` (GHSA-jcj7-v34w-v9vv) and an unauthenticated Lua interpreter path (GHSA-fq2f-crmw-q97r).

**Migration**

- `sanitize-dump-payload` and its ACL flags are deprecated no-ops upstream; the stack does not set them.

---

#### <img src="/branding/media/razzfazz-ai_knowledge_icon.png" width="20" height="20"> LightRAG

| Version | Profile | License |
|---|---|---|
| `v1.5.5 → v1.5.7` | `lightrag` | MIT |

**What's new in 2026.09**

- Upstream 1.5.6 adds a PostgreSQL-native graph backend without Apache AGE; 1.5.7 adds an end-user `/workspace` query entry with branding, a deployment-wide `USER_PROMPT_PREFIX`, and graph-first ingestion with deferred vector indexing.
- The stack's graph storage is NetworkX; the deprecated AGE adapter and the removed `GET /documents` listing endpoints are used by no stack component.

---

#### <img src="/branding/media/razzfazz-ai_chat_icon.png" width="20" height="20"> Matrix (Element Web)

| Version | Profile | License |
|---|---|---|
| `element-web v1.12.25 → v1.12.27` | `matrix` | AGPL-3.0 |

**What's new in 2026.09**

- Upstream: timeline refactoring, custom user status, room-list persistence, knock notifications; Synapse `v1.158.0` is unchanged.

**Security**

- Upstream 1.12.27 updates the bundled URL-preview implementation (GHSA-9r5h-8m2x-w7q6).

---

#### Personal agent images (Hermes, Moltis)

| Version | Profile | License |
|---|---|---|
| `hermes-agent v2026.8.3 → v2026.8.27`, `moltis 20260719.01 → 20260827.01` | `agents` *(runtime-only)* | MIT / Apache-2.0 |

**What's new in 2026.09**

- Hermes upstream: streaming conversational voice with barge-in, on-device wake words, an agent-to-agent protocol, grounded citations, a plugin SDK, `hermes import-agent` for migrating from other CLIs, and a cold start down from 14 s to 1.8 s.
- Moltis upstream: Slack live task cards and reactions, an ACP agent over stdio, a managed files library, durable calendar, channel and e-mail connectors, a vector-database memory backend.
- Both are pulled per agent instance; a user sees **Update available** on the instance card and upgrades when convenient.

**Security**

- Hermes upstream: protected instruction files require write approval, a redaction sweep across terminal errors and checkpoint logs, plugin scanning on install.
- Moltis upstream: shell and privileged tools gated behind a per-account operator list, authentication required for vault unlock and recovery, node pairing signatures verified.

---

#### Core Infrastructure

| Version | Profile | License |
|---|---|---|
| Carried forward | *(always on)* | Proprietary |

**What's new in 2026.09**

- `rzfz status` reports when file-integrity monitoring last looked, names the agent it read, and recognises the canonical LLM alias.
- A failed runner switch is no longer silent.
- A volume nobody mounts is reported, and a volume that holds only backup plumbing is separated out.
- Licence texts are vendored in-tree with provenance binding, and refreshing them is an authenticated maintainer action.
- Per-service database roles exist before the module that needs them starts.
- A thin inference node is a declared role with a node report of its own.
- An online upgrade verifies the images this box runs before it restarts the stack and refuses on a missing one, instead of swallowing pull failures three ways (#2035).
- `ollama-proxy` no longer crash-loops between `rzfz init` and post-install; it starts on a value that cannot authenticate and is re-keyed by post-install (#2036).
- A default that changes in `.env.example` between releases needs a manifest rule or a note saying why not — guarded in the tests and in the release pre-flight (#2037, #198).
- The manifest records the versions that actually ship, every Dockerfile `FROM` is pinned, and a fleet-wide guard holds compose to the manifest (#1969).
- Enabling the legacy GPUStack backend from the Portal applies the device overlay and the non-root preparation itself, and measures the effect (#1973).
- A wildcard weight name in the catalogue is refused where it cannot be attributed to a model, and the operator is told which entry (#2233).
- The posture report checks for passwordless sudo grants and says "unverified, run as root" when it cannot read the sudoers files (#2210).
- An upgrade whose host hardening cannot run defers it with a warning instead of aborting, and the verify suite's Authentik probe tells an exec failure from a service failure (#2216, #2213).
- The CLI is linked onto the path without passwordless sudo, falling back to the user's own bin directory (#1941).

**Security**

- The Wazuh signing key reaches apt as a keyring rather than an armoured file.
- Repeated proxy headers and redirect rewriting were hardened across the agent and MCP proxies.

---

## New and reworked operator surfaces

The 2026.09 cycle rebuilt the razzfazz.ai apps around one identity: the same background, the same dark navigation rail, the same information-first layout that the LLM Console introduced.

### LLM Console (`llm.<domain>`)

- Deploy a model from the catalog or the Hugging Face browser; the manager picks quantisation and computes the footprint before placing it.
- The dashboard's curves run every second over the last five minutes and follow the measurements, not the poll interval.
- Scaling a deployment can name the target worker instead of relying on auto-placement.
- The model editor keeps backend parameters it cannot display instead of deleting them on save.
- Multi-token prediction is offered where the model supports it, with the memory cost shown before enabling.
- Fleet, hub, keys, usage and playground pages each have their own how-to in the Help Center.

### Agent Manager (`agents.<domain>`)

- The portal shows one card per running instance, and a user may run several agents of one type where the tier allows it.
- A pane opens on the agent's own web UI; the raw terminal moved to the settings of the instance.
- Users name their instances, and the name follows them to the start portal tile and the terminal title.
- Administrators start, stop and resize every user's agents; every such action is written to the audit log.
- Agent classes can be fenced behind a default-deny egress proxy with an explicit allow-list.
- Personal MCP integrations — Moco/SEQITracker, Pipedrive, GitHub, Google Workspace — live in the same portal and reach every agent of the user.

### Start portal (`start.<domain>`)

- Redesigned from the user flow: tiles with equal heights, favourites and categories on the rail, light and dark.
- Curated per-user shortcut tiles with an editor: deep links into Chat, Dify and the agents, groups, a model picker.
- Tiles for Wazuh, OpenUEM and the LLM Console; the account menu deep-links to the settings pane it names.

### Settings portal (`settings.<domain>`)

- Moved from `config.<domain>`, with the old name kept as a redirect for one cycle.
- Redesigned as a dense operator tool with a master-detail settings pane.
- Toggles and maturity badges for the five new profiles; enabling GPUStack later repairs what it needs and measures it.
- The agents' global memory budget and per-user cap live here and take effect without a restart.
- Post-install reminders and readiness reports say what is missing rather than reporting a bare failure.

### Help Center (`help.<domain>`)

- Two-level navigation by topic, local full-text search, and the razzfazz look.
- Every module's upstream documentation is mirrored offline; sites that cannot be crawled are captured as one self-contained page, and a curated box-local page is served where no capture is possible.
- Own guides for the LLM Manager and the MCP & Agent Manager under *Apps & Modules*; box-local pages for Wazuh, OpenUEM and Chat beside their mirrors.

### Licenses & Attribution (`license.<domain>`)

- Tier cards, licence-family chips and category navigation replace the dense tables.
- Licence texts open in an in-site side pane and ship vendored in the tree, so a build no longer depends on GitHub.
- The header shows the box's actual current version.

---

## Upgrade instructions

### From `2026.08-ga.15` to `2026.09`

This is a supported big-bang upgrade. Run it from the stack directory:

```bash
rzfz upgrade --target v2026.09-ga
```

The upgrade takes a pre-upgrade backup, updates the code, applies the **242
environment migrations** recorded for `2026.09-rc1` and the one recorded for
`2026.09-rc4` (the security-monitoring dashboard's login mode, only where the
old default was never changed), syncs the Dify environment,
rebuilds or pulls images as needed, restarts the stack and verifies health.

**Before you upgrade, tell your API-key holders.** Every consumer of the
OpenAI-compatible API will fail with a `401` until it is given a new
`rzfz-sk-…` key, and that key cannot be minted until the box is on 2026.09.
See [Breaking changes](#breaking-changes).

### Boxes on a pre-reorg release

A box still on **2026.04, 2026.05 or any 2026.06-ga.x** must first cross the
2026.07 reorg with the reorg-aware bootstrap
(`razzfazz-upgrade-from-2026.04-GA.x.sh --target v2026.07-… --force`), then
upgrade normally. The plain upgrade path breaks across that reorg.

### Incremental upgrades within the cycle

```bash
rzfz upgrade                # to the newest release on the box's channel
rzfz upgrade --check        # dry run: show what would change
```


### Rollback

```bash
rzfz upgrade --rollback
```

Restores the pre-upgrade backup taken at the start of the run.

---

## Security

### Audit findings

The release security assessment for this cycle is produced by the security
review against the GA tag. The findings closed and the accepted residuals are
listed there; this section is completed from that run and is **not** written
ahead of it. Customers who need the assessment for an audit should ask their
razzfazz.ai support contact.

### Posture changes in this cycle

- Backend credentials no longer authenticate customer-facing LLM requests; the manager key is the only accepted credential and carries revocation.
- GPUStack runs non-root by default on AMD/Vulkan boxes.
- The Wazuh certificate tool is checksum-verified at build time instead of downloaded at container start.
- Wazuh manager-to-agent remote command execution is disabled by default, and agent auto-enrolment ships off pending the assessment.
- The observability module's outbound product analytics is disabled by default.
- Personal-agent classes run behind a default-deny egress proxy with an internal fence overlay.
- A new compose environment key cannot ship without documentation, enforced in the gate.

### CVE deltas

Nineteen image pins moved against `v2026.08-ga.15` (the per-module tables above
name each old and new version — Authentik, Dify and its plugin daemon, Open
WebUI, Komodo, Gitea, LightRAG, Gotenberg, Crawl4AI, Valkey, Docling,
Vaultwarden, Stirling-PDF, Element Web, cognee-mcp and the agent images), and
seventeen images are new to the stack. The image-level CVE delta is therefore
real and is computed at the GA cut by the release security review (SBOM plus
CVE diff against the previous GA); its NEW-CRITICAL / NEW-HIGH gate blocks the
tag mechanically, and the resulting table is inserted here from that run rather
than written ahead of it. No further bump was available at the cut: every
third-party image already sits on its newest published immutable tag.

---

## Known issues at GA

- **cognee-mcp ships a vendor pre-release (`1.5.3.dev1`).** Docker Hub carries no stable `1.5.3` or `1.5.4` image of `cognee/cognee-mcp` although the cognee backend released both; the highest stable image tag is `1.5.0`. The manifest now records the tag that ships (the previous record named an image that cannot be pulled). Accepted for rc1 and GA as a conscious decision; re-checked at the next cycle's upstream pass. If you must avoid pre-release images, pin `COGNEE_MCP_VERSION`-equivalent `1.5.0` in `modules/knowledge/cognee/compose.yml` and the authshim Dockerfile together.
- **GPUStack (the optional legacy inference module) ships with an unfixed authentication flaw.** Its SAML callback accepts a login assertion without verifying the signature, so anyone who can reach that endpoint obtains a session as any GPUStack user; the route answers whether or not SAML is configured. Upstream fixed it in v2.2.2 and we stay on v0.7.1 deliberately — the pinned build carries the AMD Vulkan and CUDA sm_120 work this stack depends on. The module is **off unless you enable it**, the LLM Manager is the default backend and is unaffected, and the reverse proxy keeps the endpoint off the internet; it is reachable from inside the box network, including from Dify workflows. The module's own description states this where you enable it. Accepted residual risk, disclosed.
- **Wazuh's dashboard uses a local admin login, not the box's single sign-on.** The module ships in `WAZUH_AUTH_MODE=forward_auth`: you pass the box's Authentik gate and then sign in to the dashboard as `admin` with `WAZUH_INDEXER_PASSWORD` from the box's `.env`. The experimental `native_oidc` mode (Authentik as the identity inside the dashboard) loops on 401 at its callback and is not the default until that is fixed. The SIEM itself — manager, indexer, agents, alerts — is unaffected.
- **On-demand file-integrity rescans do not work for the manager's local agent.** Both documented handles report success and leave the timestamp untouched; remote agents are unaffected. Wait for the schedule or restart the manager.
- **crawl4ai's bundled playground** cannot render its own streamed response; the crawl itself succeeds and the API is unaffected.
- **LiteLLM re-reads its routing file on its own schedule**, so a brief lag between a correct configuration and a serving router remains after a switch.
- **Dify indexes the body of an Outlook `.msg` but not its attachments.** Upstream installs `unstructured` without its parser extras and skips each attachment with a warning; the fix (the extras in the API image) grows that image by 8.6 GB and is deferred to 2026.10 by operator decision — three of the 37 PSA test documents are affected.
- **`rzfz init --force` over a previous installation's volumes under a different `MAIN_DOMAIN` produces a box that looks installed but is not.** `--force` keeps the data volumes (only `--factory-default` clears them), so three core reconcilers exit against databases of the earlier domain and cognee never starts, with no message naming the cause. To re-install under a new domain, run with `--factory-default` (deliberate data loss) or keep the previous domain.
- **The Help Center opens the upstream mirror's "Index" page for Wazuh, OpenUEM and Open WebUI** instead of the curated module page; the curated page is one link away on that page.
- **The package completeness check does not know the hardware rule.** It expects every catalogue model of every profile, while three chat models are on-demand spares on a CPU box by design, so a CPU-class package that is complete for its class is reported incomplete and is written with the documented override; its count is also wrong by one in each term. (#2234)
- **An offline upgrade does not switch to the new release's upgrade script
  mid-run.** The release being left drives the whole upgrade; a fix to the
  upgrade path takes effect on the following hop. For releases before
  2026.09-rc6 the package must be placed at the appliance path first (see the
  upgrade instructions). (#2260)
- **A CPU box on the legacy inference runtime chats but cannot embed.** The
  chat default has a hardware rule since this cycle; the embedding and
  reranker defaults do not, so such a box is asked to serve an embedding
  model it cannot place and every RAG consumer wired to it gets "invalid
  model name". Boxes on the previous release could not embed on that hardware
  either; a box on the LLM Manager embeds. The per-hardware rule for
  embedding and reranking is a 2026.10 change, and the data-preserving move
  from the legacy runtime to the LLM Manager is the planned path. (#2262)
- **Stopping the stack leaves the model engines running.** They are started outside the compose project and keep the models volume in use; `rzfz stop` leaves them answering and a teardown cannot remove the volume. Removing them by hand is not a state either: the node agent recreates them within about half a minute unless it is stopped first. (#2226)
- **The Start Portal hides the Workflow Automation tile from members of "razzfazz.ai Workflow Automation Users".** The tile is marked administrators-only by the portal's taxonomy even though the group admits its members to Dify; they can open `dify.<domain>` directly. Whether the tile or the group changes is an open decision. (#2246)
- **API tokens created in Authentik live thirty minutes whatever expiry is requested**, and any edit renews the thirty minutes. Authentik caps an API token's lifetime at the user's or group's maximum token lifetime, which this release sets nowhere, so the default applies and the expiry field in the form has no effect. Expiry itself is enforced. Setting a longer maximum for administrators and automation users is an open decision. (#2247)

---

## For engineers

- [Upstream changes per module](module-upstream-changes.md) — the research behind every module section (versions, highlights, breaking changes, security notes).
- [.env upgrade check per module](env-upgrade-check.md) — what each bump could have required and what it did.
- [Every closed issue of the cycle](closed-issues.md) — one line per issue, grouped, regenerated at the GA cut.
Chronological detail for this cycle is in [`changelog.md`](changelog.md); the
30-second summary is in [`WHATS_NEW.md`](WHATS_NEW.md).
