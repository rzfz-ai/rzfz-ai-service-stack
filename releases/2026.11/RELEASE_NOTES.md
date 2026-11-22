# Release Notes — 2026.11

**Cycle window:** 2026-09-29 → GA · **GA tag:** `2026.11-ga` (date to be set at tagging)
**Previous GA:** `2026.09-ga.5`

---

## Executive summary

2026.11 is the cycle in which the box stopped trusting its own parts by default.
Application services now receive only the settings they read instead of the whole stack
`.env`, no personal or coding agent holds the host's Docker socket, credentials
stay out of logs and, with one named exception, off command lines, containers can no longer reach a cloud
server's metadata address, and the local model engines run non-root on a
read-only file system, with the upgrade moving running engines onto them. Alongside that hardening, access to knowledge now follows
the person: one role catalogue drives who reads which knowledge in Chat, Dify and
Cognee, and Microsoft and Google sign-ins bring their group memberships with them.

The second line of work made the agent side of the box a product. The **MCP &
Agent Manager** is a production module, the MCP gateway connects Jira and
Confluence, Microsoft 365, Gitea and Docling to every agent and to Chat, each
acting as the signed-in person, and the new **OpenBao** module keeps those
connections and each person's secrets. The **LLM Manager** places models
honestly on small and mixed hardware, follows llama.cpp's stable releases, serves
the OpenAI Responses API, notices within seconds when a Linux worker went away and lets
a Mac join the fleet as a native worker. The cycle also takes the upstream
updates of the bundled services (GPUStack stays on 0.7.1), including five larger steps: Gitea 28,
paperless-ngx 3, Stirling-PDF 3, OpenLIT 2 on ClickHouse 26.3 and Onyx 4.9.

Upgrading from `2026.09-ga.5` is a supported one-step upgrade carrying **153
environment migrations**; the upgrade now stops when its own backup fails and
never lowers a service whose data has moved forward. Several upstream steps
migrate their databases one way, OpenHands is removed, and Dify's outbound proxy
refuses more address ranges, so read [Before you upgrade](#before-you-upgrade).
Customer boxes upgrade from the public release channel from this release on (see
[Upgrade notes](#upgrade-notes)). The security posture of the GA tag is set by
the release security review against exactly this pin set.

## Highlights

What a customer sees first. Each item has its detail further down.

- **Stack secrets reach only the application services that read them**, and credentials no longer appear in logs or, with one named exception, on command lines.
- **Knowledge follows the person:** knowledge scopes decide who reads which data in Chat, Dify and Cognee, and removing someone from a scope takes effect in Dify and Cognee within about an hour and in Chat at the person's next sign-in.
- **Microsoft and Google sign-in carry group memberships** into the box at every sign-in, and an unverified e-mail address can no longer take over an existing account.
- **The MCP & Agent Manager is a production module**, with Jira & Confluence and Microsoft 365 (read-only), both experimental, and Gitea and Docling behind one MCP gateway that acts as the signed-in person.
- **OpenBao** (experimental) is the new home for secrets and per-person tool connections, and Infisical is deprecated.
- **The LLM Manager** explains and lets you choose where a model runs, notices a lost Linux worker within seconds, caps runaway requests and serves the OpenAI Responses API.
- **llama.cpp v0.9.0**, a stable release, runs the local engines on AMD (Vulkan), NVIDIA GB10 and CPU workers, and the upgrade moves the box's running engines onto it.
- **A Mac can join the fleet as a native worker**, with its own Ollama behind the worker agent.
- **Upgrades, backups and restores stop instead of pretending:** from 2026.11 on a failed backup stops the upgrade, a restore reports every volume it could not bring back, and the nightly backup waits for a running knowledge import before it stops the knowledge services.
- **Offline boxes stay quiet:** search, sign-on, documents and the help mirror make no outbound lookups on a box set to offline, and `scripts/harden-offline-host.sh` quiets the host's own background lookups.
- **Five larger upstream steps:** Gitea 28, paperless-ngx 3, Stirling-PDF 3, OpenLIT 2 with ClickHouse 26.3, and Onyx 4.9.
- **OpenHands is no longer part of the stack**; use opencode, Codex or the User Defined Agent for coding work.

---

## Table of contents

- [Highlights](#highlights)
- [Before you upgrade](#before-you-upgrade)
- [Cross-cutting changes](#cross-cutting-changes)
  - [Least privilege by default](#least-privilege-by-default)
  - [Knowledge follows the person](#knowledge-follows-the-person)
  - [Agents and tools as the signed-in person](#agents-and-tools-as-the-signed-in-person)
  - [An LLM fleet that says what it does](#an-llm-fleet-that-says-what-it-does)
  - [Upgrades, backups and restores that stop instead of pretending](#upgrades-backups-and-restores-that-stop-instead-of-pretending)
  - [Offline boxes stay quiet](#offline-boxes-stay-quiet)
- [Module-by-module changes](#module-by-module-changes)
- [Operator UX: Settings portal and apps](#operator-ux-settings-portal-and-apps)
- [Fixed since 2026.09-ga.5](#fixed-since-202609-ga5)
- [Upgrade instructions](#upgrade-instructions)
- [Security](#security)
- [Known issues at GA](#known-issues-at-ga)

---

## Before you upgrade

These changes need a decision or a short action from the operator; each one is
repeated in its module section.

- **Dify workflows can no longer call the box's own public addresses**, and carrier-grade NAT (`192.0.2.10/10`) and benchmarking (`198.18.0.0/15`) addresses are refused; list needed ranges in `DIFY_SSRF_ALLOW_RANGES` and use the box's models through Dify's model picker.
- **A key you added to `.env` by hand reaches a container only if that service names it**; pass it through a compose override.
- **Several upgrades are one-way:** paperless-ngx 3, Onyx 4.9, Gitea 28, Stirling-PDF 3, Synapse, Vaultwarden, Cognee and (with observability on) OpenLIT 2 with ClickHouse 26.3 migrate their data, so going back means restoring the pre-upgrade backup.
- **OpenHands is removed**; its data volumes are kept and listed by the upgrade, and the Gitea token it used should be deleted afterwards.
- **The Valkey password is rotated once** by the upgrade, which briefly restarts the services that use it.
- **Cognee asks everyone to sign in again once**, and personal tool connections saved before 2026.11 that are not tied to the person's account id are connected again once.
- **The Settings portal's update check needs a read-only token** in `STACK_UPDATE_TOKEN` where the repository does not allow anonymous reads.
- **On 2026.08 and 2026.09, take a verified backup by hand before `rzfz upgrade`**: the backup the upgrade takes first can be incomplete and still report success (see [Upgrade instructions](#upgrade-instructions)).
- **Personal agents created before 2026.11 need to be recreated once** (Update or Reinstall on each in the MCP & Agent Manager; stopping and starting is not enough) so they trust the box's own certificate authority.
- **The Ollama-compatible endpoint now requires a key and no longer listens on the box host** (`127.0.0.1:11434`); a client you pointed at it sends the key from `OLLAMA_PROXY_CLIENT_KEY` or moves to the LLM Manager endpoint (see [Upgrade notes](#upgrade-notes)).
- **A compose overlay outside the stack directory** keeps working but cannot be read by the Settings portal, and `rzfz status` warns about it; move it into the stack directory (see [Upgrade notes](#upgrade-notes)).
- **Set `RAZZFAZZ_CHANNEL=public` on a customer box before the upgrade**; from 2026.11 on, a box left on the internal release channel is refused an online upgrade (see [Upgrade notes](#upgrade-notes)).

---

## Cross-cutting changes

### Least privilege by default

The largest change of the cycle, and the one most of the module notes hang off:
each part of the box now holds only what it needs.

- No application service loads the whole stack `.env` any more; each one receives only the keys it reads, so a compromised application container no longer holds every stack secret. The management services that edit or back up the configuration (Settings portal, Start portal and backup) still read the file, and Dify's services read their own `.env.dify`.
- No personal or coding agent holds the host's Docker socket, and Moltis starts without the socket, without `sudo` and without the Docker group.
- On thin worker nodes a model engine can no longer reach the Docker API, and the node agent's control API requires the node credential.
- The local model engines run non-root (on AMD once the GPU group ids are known), with all capabilities dropped, a read-only root file system, a process limit and their weights mounted read-only. At its end the upgrade relaunches the box's running engines onto this release's runner and settings, with a health check and a fallback to the previous runner for an engine that does not come up; `--no-engine-relaunch` leaves them as they are, `rzfz relaunch-engines` moves them later, and an engine on a remote worker moves with the runner upgrade in the LLM Console.
- A model file is structurally checked before an engine opens it, and the catalogue pins every model to a Hugging Face commit and a checksum per file.
- A model that is not a verified catalogue entry shows its risk and deploys only after an explicit acknowledgement.
- Backup and database passwords, the Open WebUI and Dify tokens, the worker enrolment token and administrator passwords no longer appear on a command line or in a server log; the one exception is the administrator account the Gitea container creates at its very first start.
- Role passwords reach PostgreSQL as a client-side verifier instead of in plain text, and every service connects with its own database account instead of the administrator account.
- The Caddy admin API is no longer reachable from user-code containers or from the network.
- Containers on the stack host and on thin LLM workers can no longer reach cloud metadata addresses, and blocked attempts are logged on the host (rate-limited).
- On a new installation Open WebUI starts with its code interpreter switched off, and with direct tool servers and the creation of tools limited to administrators.
- Open WebUI no longer holds the Cognee administrator password; Cognee knowledge is reached through per-user tools that sign in as the user.
- The published default logins of OpenLIT and Cognee are replaced or locked when their module is enabled, and the result is checked with a real sign-in.
- A fresh installation hardens its host before the stack starts, instead of at its first upgrade; where that needs a password the installer cannot ask for, its final summary names the command to run.

### Knowledge follows the person

Who may read which knowledge used to be decided separately by each app. It is
now decided once.

- One role catalogue drives administrator rights and user tiers in the Settings portal, Start portal, Help Center, Agent Manager, MCP manager and LLM Manager.
- A group marked as a knowledge scope restricts the Dify and Cognee datasets and the Open WebUI knowledge of its scope to the scope's members and their owner.
- The Settings portal shows who can read each knowledge scope.
- Removing someone from a knowledge scope takes effect in Dify and Cognee within about an hour, through a reconcile that runs every hour and writes each change to the audit log.
- Role changes and disabled accounts in Dify, Cognee and Matrix follow on the same hourly run, and Open WebUI follows at the person's next sign-in.
- Microsoft (Entra) and Google sign-ins bring the person's upstream group memberships into the box at every sign-in once the groups are mapped; groups with administrator rights are never assigned by the sync, and when the group lookup fails, synced memberships are kept for at most 24 hours.
- `rzfz status` reports a knowledge scope that someone outside it can still read, and whether the Microsoft or Google group sync holds.
- Existing public knowledge in Open WebUI becomes private to its owner once, so nothing is shared by accident when scopes are introduced.

### Agents and tools as the signed-in person

The agent side of the box became a product, built around one rule: a tool acts
as the person who uses it, never as a shared account.

- The MCP & Agent Manager and its MCP integrations are production modules; the Jira & Confluence and Microsoft 365 connectors behind the gateway are experimental.
- The MCP gateway connects Jira & Confluence, Microsoft 365 (read-only), Gitea and Docling to every agent and to Chat, each through the person's own connection, and Dify workflows reach the gateway through a technical user.
- Chat tools act as the person by their Authentik sign-in, which the gateway verifies against Authentik's keys, instead of trusting an e-mail address Open WebUI reports.
- With OpenBao on, the MCP manager keeps each person's tool connections in OpenBao, and agents read their owner's secrets through a read-only proxy with a token that is revoked with the owner.
- Technical users let an automation or an autonomous agent reach the gateway and Gitea under its own restricted identity, with a token shown once and revoked immediately.
- Gitea can serve as a knowledge base through the gateway, with writes limited to branches.
- A deleted person's stored tool connections are erased at once, and a deactivated person's after 30 days.
- After an upgrade, each personal agent still running an older image shows an "Update available" badge, idle agents are updated automatically and `rzfz agents update` updates the rest.
- Running personal MCP tools move to the current version by themselves within 15 minutes of an upgrade.

### An LLM fleet that says what it does

The LLM Manager introduced in 2026.09 grew into a fleet manager that explains
itself on small and mixed hardware.

- A model that cannot fit is refused with a reason, an unknown memory budget is never treated as unlimited, and admission counts the measured footprint of the shipped models.
- Each placement records the candidate workers, their free memory and why the chosen one won, and an administrator can make a deployment prefer or pin a worker.
- A remote worker on the current worker agent that goes down stops counting as ready within about eight seconds (a Mac worker within 90 seconds), so new requests go to the model's other instances.
- A model that is briefly unavailable answers `503` with `Retry-After` instead of "invalid model name", so applications retry.
- Every request gets a per-model ceiling on generated tokens, and a generation stops as soon as the application that asked has given up.
- `https://llm.<your-domain>/v1/responses` serves the OpenAI Responses API (without stored responses) with the same keys, limits and metering as chat completions.
- The engines follow llama.cpp's stable releases from v0.9.0 on, and the upgrade moves the box's running engines onto the release's runner.
- NVIDIA GB10 and DGX workers join and update from an amd64 master, and `rzfz node-upgrade` verifies the new worker agent and rolls back if a model is lost.
- A runner upgrade checks every model first and starts a model that fails on the new runner again on the one it ran before.
- A Mac joins the fleet as a native worker, with a managed Ollama on its own port and a self-upgrade with checksum check and rollback.
- A 16 GB CPU box gets an embedding model that fits, and a CPU box that still ran GPUStack moves to the LLM Manager's own engines during the upgrade.

### Upgrades, backups and restores that stop instead of pretending

A step that fails now says so and stops, instead of reporting success.

- From 2026.11 on, a failed pre-upgrade backup stops the upgrade, and `--skip-backup` is the explicit way on.
- An upgrade never lowers a service that migrated its data, a restart loop fails the upgrade, and a rollback keeps the newer version of a service whose data moved forward and names it.
- `rzfz upgrade` and `rzfz init` end with a health check and fail while a core or enabled service is down, and the upgrade then does not record the new version.
- An offline upgrade that lacks images the new release needs stops before it changes anything.
- A restore replaces a volume instead of merging into it, reports every volume it could not restore, and restores agent and engine volumes by their owner.
- Rotating the backup password keeps the previous three generations, so earlier archives stay restorable, and `rzfz backup list` names the generation of each archive.
- The nightly backup checks its disk space first, clears unfinished and expired archives before the newest complete one, and stops with the numbers if space is still short.
- The scheduled backup waits for a running upgrade, and up to two hours (configurable) for a running Cognee or LightRAG import, before it goes ahead; it names any work it then interrupts.
- An archive that began before its preparation step had finished is never marked complete, so `rzfz status` and the next upgrade do not rely on it.

### Offline boxes stay quiet

An offline box should behave like one, and keep the capabilities it had online.

- Speech-to-text and text-to-speech models are carried to an offline box and installed by every upgrade where they are missing.
- Search, sign-on, document and gateway services no longer try to reach the internet for update pings, analytics or list downloads.
- The Help Center no longer contacts public test hosts to decide whether the box is online.
- Dify knowledge bases index on an offline box, and Cognee processes text without downloading an encoding table.
- On an offline box the host no longer looks up package changelogs, and the Cognee web app no longer runs npm's update check.
- The time source is a setting (internet, DHCP, your own LAN servers or none), and the offline firewall opens the time port only to the servers you name.
- Switching a box back online refuses while the offline firewall is still active and names the command that removes it.

---

## Module-by-module changes

### Core

#### Core Infrastructure

| Version | Profile | License |
|---|---|---|
| Bumped during the cycle | `core` | Proprietary |

Sub-images bumped together with the core:

| Sub-image | Version |
|---|---|
| Authentik | `2026.5.7 → 2026.8.5` |
| Caddy | `2.11.4 → 2.11.7` |
| Valkey | `9.1.2 → 9.1.3` |
| PostgreSQL with pgvector | `0.8.2 → 0.8.6` (PostgreSQL 17) |
| Backup service (docker-volume-backup) | `2.48.2 → 2.50.0` |

**What's new in 2026.11**

- Authentik moves to its 2026.8 line, and the upgrade walks the release series in order, so a box on Authentik 2026.2 passes 2026.5.7 on the way.
- Caddy canonicalises the path that remains after a prefix strip, so a crafted path can no longer slip past a path-based rule.
- Caddy accepts request headers up to 32 KiB and drops header names that contain a dot.
- Caddy's memory limit is 2 GB by default and can be set with `CADDY_MEM_LIMIT`, and `rzfz status` warns when the proxy gets close to it.
- Caddy no longer requests certificates for the GPUStack and Matrix names while those modules are switched off.
- When Caddy cannot connect to a service, for example because a name lookup inside Docker takes too long, it tries again for up to 5 seconds instead of answering 502 at once. Only a connection that was never made is retried, so a request a service already received is never sent twice and a running stream is never restarted (the optional legacy GPUStack routes also retry a read request after a reset connection).
- `APEX_TLS=internal` puts the apex name on the box's own certificate authority for boxes whose apex DNS points elsewhere.
- pgvector 0.8.6 fixes index corruption during HNSW vacuum, and the upgrade updates the extension in every database that has it.
- A backup to an SSH target needs the target's host key before it connects.
- `RAZZFAZZ_AUTO_REBOOT=false` keeps the automatic reboot after security updates off across later hardening runs.
- `rzfz module` prepares and enables Matrix from the command line and resets a module's data when its key has changed.

**Security**

- The Caddy admin API is no longer reachable from user-code containers or from the network, and an unauthenticated `OPTIONS` request can no longer start or reach a personal agent, and identity headers a client sends itself are stripped on every site.
- Admin multi-factor sign-in, once switched on with `RAZZFAZZ_REQUIRE_ADMIN_MFA=true`, asks a Super Admin without an authenticator to enrol at the next sign-in.
- Every service connects to PostgreSQL with its own account, which owns only that service's databases; only the database server, its maintenance step and the backup keep the administrator account.

**Migration**

- `MATRIX_SERVER_NAME`, `TIME_SOURCE`, `CADDY_MEM_LIMIT`, `RAZZFAZZ_AUTO_REBOOT` and the other new settings are added with defaults that keep today's behaviour, except that Caddy's memory limit rises from 512 MB to 2 GB.
- `RAZZFAZZ_REQUIRE_ADMIN_MFA` stays off on a new installation so a box can be handed over, and an upgrade keeps the value an existing box has.

### AI & Chat

#### <img src="/branding/media/razzfazz-ai_chat_icon.png" width="20" height="20"> Chat (Open WebUI)

| Version | Profile | License |
|---|---|---|
| `0.11.3 → 0.12.4` | `chat` | MIT |

**What's new in 2026.11**

- Two-factor sign-in, end-to-end encrypted channel chats, external apps in channels, realtime voice calls and Tool Search are available and stay off until an administrator turns them on.
- Each refresh lists the retrieval settings it changed, and `RAZZFAZZ_OWUI_RETRIEVAL_MANAGED=false` keeps an administrator's own retrieval settings.
- A long-document preset that reads the whole document and cites clauses, and a typing indicator, ship with the box.
- Chat models of a worker that is briefly unreachable keep their descriptions, parameters and access settings instead of being re-added with defaults.
- The "Sign in with Authentik" button appears only when single sign-on is switched on.
- Tool connections added by the MCP module are removed when the module is switched off, and connections an administrator added stay.

**Security**

- The setting that stops administrators reading other people's chats is enforced on every route, and a password change signs the account out everywhere.
- An identity provider can no longer delete administrator accounts through directory sync.
- On a new installation the code interpreter, direct tool servers and user-created tools are off for everyone except administrators.

**Migration**

- An existing installation keeps the tool and code-interpreter settings it has saved; a setting it never saved takes the new restrictive default. The upgrade prints the result and the Settings portal's Chat page shows it.
- A workspace model whose id contains a space or a tab keeps answering but cannot be saved again until it is recreated.
- The "Cognee Memory Pipe" model is removed from Open WebUI by the upgrade.

### Workflows

#### <img src="/branding/media/razzfazz-ai_workflow_icon.png" width="20" height="20"> Workflows (Dify)

| Version | Profile | License |
|---|---|---|
| `1.17.1 → 1.18.0` | `dify` | Apache-2.0 |

Sub-images bumped together with the main image:

| Sub-image | Version |
|---|---|
| dify-plugin-daemon | `0.6.10-local → 0.6.12-local` |
| dify-sandbox | `0.2.15` (carried forward) |

**What's new in 2026.11**

- Dify's additive database migrations run when the API starts, and the API image now uses Python 3.13.
- Structured output works with the stack's models on every installation, and `rzfz status` proves it with one structured-output request through Dify.
- Models served by a worker node keep their structured-output, image-input and thinking settings across upgrades.
- "Thinking off" also reaches the engine for streamed requests, as Dify's LLM nodes send them.
- Dify reaches the stack's MCP gateway through its outbound proxy.
- Dify's new Agent Knowledge Base, agent backend and agent sandbox are not part of the stack.

**Security**

- Dify's outbound proxy refuses carrier-grade NAT, benchmarking, documentation, reserved and multicast ranges, NAT64, 6to4 and Teredo prefixes, IPv4-compatible IPv6 addresses and the box's own addresses.
- A host name is checked against the address it resolves to, and the chain proxy of a corporate-proxy box refuses the same set.
- The plugin daemon runs as an unprivileged user, refuses plugin packages whose entries escape the extraction folder, and no longer passes its environment to plugin processes.
- `PASSWORD_PEPPER` stays empty whatever `.env.dify` says, because the stack writes Dify password hashes itself.

**Migration**

- An HTTP Request node that called one of the box's own host names now gets HTTP 403; use the box's models through Dify's model picker instead.
- On a box behind a CDN or load balancer, set `DIFY_SSRF_DENY_BOX_OVERRIDE`, and list the carrier-grade NAT or benchmarking sub-ranges (inside `192.0.2.10/10` or `198.18.0.0/15`) your workflows really need in `DIFY_SSRF_ALLOW_RANGES` before recreating Caddy.
- If your network uses a NAT64 prefix other than `64:ff9b::/96`, add it to `DIFY_SSRF_DENY_HOSTS`.
- The upgrade re-owns the plugin daemon's storage volume once and adds a small volume for its token encoding.

### LLM Inference

#### LLM Manager (manager)

| Version | Profile | License |
|---|---|---|
| Rebuilt during the cycle; router LiteLLM `main-v1.83.7-stable → v1.108.4` | `llm-manager` | Proprietary |

**What's new in 2026.11**

- The endpoint serves the OpenAI Responses API at `/v1/responses`, statelessly, with the same keys, limits and metering as chat completions.
- Placement records why a worker was chosen, and the console's Placement panel lets an administrator prefer or pin a worker per deployment.
- A worker that stops sending its two-second signal stops counting as ready within eight seconds (`LLM_MANAGER_WORKER_SILENCE_SECONDS`).
- Every request gets a ceiling on generated tokens from the model's own maximum, at most `LLM_MANAGER_MAX_TOKENS_CEILING`, also when it uses llama.cpp's own parameter names.
- A generation stops when the application disconnects or times out, and the usage statistics record why each request ended.
- A briefly unavailable model answers `503` with `Retry-After: 15`, and an embedding model refuses chat requests with `400` naming the right endpoint.
- The console shows the engine build each model instance runs, and multi-token prediction can be set from one to four draft tokens in the model editor.
- Models with an egress risk carry a badge, and models on the excluded list cannot be deployed.
- A worker agent on another release line is flagged on the Fleet page and in `rzfz status`.
- Deleting a deployment stops its pending download and removes the files fetched for it.
- The registry cleanup removes only cached models and leaves the worker and runner images alone.
- The model sync deletes only entries it created and shows its plan with `--plan`.

**Security**

- LiteLLM moves past the Host-header authentication bypass CVE-2026-49468 and now refuses to start without a real master key, which the stack generates.
- A catalogue model is pinned to a revision and a checksum, and an unverified model needs an acknowledged risk notice before it deploys.

**Migration**

- Onyx gets its own key (`LLM_MANAGER_ONYX_KEY`), created by the upgrade.
- The Ollama-compatible endpoint requires `Authorization: Bearer <OLLAMA_PROXY_CLIENT_KEY>` and is no longer published on the box host; a client that used `127.0.0.1:11434` moves to `https://llm.<your-domain>/v1` with its own API key.
- The retired `llm-manager.<domain>` name stays as a deprecated alias for workers enrolled under it, and `rzfz node-upgrade --manager-url` moves them.

---

#### LLM Worker-Agent

| Version | Profile | License |
|---|---|---|
| Rebuilt during the cycle; engines llama.cpp `b9851` (CPU `b10853`) → `v0.9.0` (build `b12110`) | `llm-worker-agent` | Proprietary |

**What's new in 2026.11**

- The engines on AMD (Vulkan), NVIDIA GB10 and CPU workers run llama.cpp v0.9.0, and the upgrade moves the box's running engines onto it.
- An earlier llama.cpp build (`b11778`) stays installed on AMD workers as a rollback, and the ROCm engine is unchanged.
- The worker agent is one multi-architecture image for amd64 and arm64, so GB10 and DGX workers join and update from an amd64 master.
- `rzfz node-upgrade` records the running models, waits until the new agent lists them all and rolls back otherwise.
- A Mac joins as a native worker with a managed Ollama on `127.0.0.1:11435`, leaving the Mac's own Ollama untouched.
- An engine whose network was removed by a worker reboot is re-created on the current engine network and started.
- Engine instance names are valid host names, so a model with a long or unusual name no longer stays in "loading".
- An engine started for embeddings or reranking is recognised as such when a worker re-registers, so chat is never routed to it.

**Security**

- Engines run non-root (on AMD once the GPU group ids are known), capability-free and read-only on an internal network, and the upgrade relaunches engines that still ran with older settings. `LLM_ENGINE_HARDENING=off` is the recorded way back, and `rzfz status` checks the running engines and warns for every one that is not hardened or not on the release runner.
- The node agent's control API requires the node credential.

**Migration**

- Update each worker with `rzfz node-upgrade` after the master; `rzfz upgrade` on the master lists the workers that are behind.
- `rzfz upgrade` relaunches the box's engines at its end; a model can be briefly unavailable while its engine moves. `rzfz upgrade --no-engine-relaunch` leaves them as they are, and `rzfz relaunch-engines` moves them later.
- On AMD Vulkan, reranker scores moved by about 0.003 with the new engine in our measurement, while the order of results stayed the same.
- A Mac worker installed earlier keeps its Ollama on port 11434 until `ollama-port-migrate.sh` moves it.

---

#### LLM Model Registry (Zot)

| Version | Profile | License |
|---|---|---|
| Carried forward | `llm-registry` | Apache-2.0 |

**What's new in 2026.11**

- The registry cleanup reports a model removed under two tags, or already emptied, as done instead of failed.

**Security**

- The registry is read-only for anonymous clients: services on the stack network and the local registry port can still download models, only the LLM Manager can add, replace or delete them, and remote workers keep their sign-on.

**Migration**

- The upgrade generates the registry write credential (`LLM_REGISTRY_WRITE_PASSWORD`); a push to the local registry port by hand logs in with it first.

---

#### <img src="/branding/media/razzfazz-ai_llm_icon.png" width="20" height="20"> LLM Inference (GPUStack 0.7.1)

| Version | Profile | License |
|---|---|---|
| Carried forward | `llm-legacy` | Apache-2.0 |

**What's new in 2026.11**

- A CPU box that still ran GPUStack moves to the LLM Manager's own engines during the upgrade, and GPUStack is removed only after they serve.

**Security**

- In master mode the Ray job API and dashboard bind to loopback only, and the legacy worker ports stay on loopback by default.

**Migration**

- Models you added to GPUStack yourself go out of service on a migrated CPU box and are listed by name, while their weights stay in the `gpustack-data` volume.

---

#### Mac LLM Gateway (LiteLLM)

| Version | Profile | License |
|---|---|---|
| `main-v1.83.7-stable → v1.108.4` | `mac-llm` | MIT |

**What's new in 2026.11**

- The gateway shares the LLM Manager router's LiteLLM version, from upstream's release images pinned by digest, and stdio MCP servers are off.

**Security**

- The authentication bypass CVE-2026-49468 of the previous image is closed.

### Monitoring

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Observability

| Version | Profile | License |
|---|---|---|
| OpenLIT `1.24.1 → 2.1.1` | `observability` | Apache-2.0 |

Sub-images bumped together with the main image:

| Sub-image | Version |
|---|---|
| ClickHouse | `25.8.28.1 → 26.3.60.2` |

**What's new in 2026.11**

- OpenLIT 2.1 adds a per-trace view, filters for truncated or looping model calls and trace governance reports.
- An idle box no longer stores Dify's background database and cache activity as traces.
- The configured retention also reaches the stored metrics.
- Observability data is backed up again, and backups are no longer reported as degraded because of it.

**Security**

- OpenLIT's published default login is locked and stays locked across restarts; enabling or refreshing the module proves it with real sign-ins, and `rzfz status` checks that it stays locked.

**Migration**

- The upgrade is one-way: ClickHouse 26.3 writes data the previous version cannot read, so going back means restoring the pre-upgrade backup.
- OpenLIT converts its settings and marks stored traces with their project once at first start, which briefly uses extra disk space.

---

#### <img src="/branding/media/razzfazz-ai_admin_icon.png" width="20" height="20"> Monitoring (Komodo)

| Version | Profile | License |
|---|---|---|
| `2.3.3 → 2.3.4` | `monitor` | GPL-3.0 |

**What's new in 2026.11**

- The host agent sits on an internal network and its lookups of public DNS resolvers can no longer leave the box, and its port 8120 is no longer published.
- Komodo's nightly automatic image cleanup is switched off again by `rzfz post-install --refresh` too, so a factory reset no longer leaves it switched on.

**Security**

- Komodo stops trusting a certificate-authority root that upstream withdrew after a key compromise.
- Komodo's own database has its own password (`KOMODO_PG_PASSWORD`), so the monitoring services no longer hold the main database's administrator password; a box is moved over automatically at the module's next start.
- The stack builds Komodo's database address itself, and the upgrade removes the `KOMODO_DATABASE_URI` setting from `.env`.
- The monitoring database's debugging endpoint is reachable only inside its own container.

### Search

#### <img src="/branding/media/razzfazz-ai_search_icon.png" width="20" height="20"> Search (SearXNG)

| Version | Profile | License |
|---|---|---|
| `2026.7.19 → 2026.9.23` | `searxng` | AGPL-3.0 |

**What's new in 2026.11**

- The metasearch keeps its queries on the box's outbound proxy, query suggestions off and the previous form method.

---

#### <img src="/branding/media/razzfazz-ai_search_icon.png" width="20" height="20"> Crawl4AI

| Version | Profile | License |
|---|---|---|
| `0.9.3 → 0.9.5` | `crawl4ai` | Apache-2.0 |

**What's new in 2026.11**

- Crawl4AI is a production module.

**Security**

- Nine upstream advisories are closed, among them three ways to make the server request internal addresses and a bypass that exposed server environment variables.

**Migration**

- A site with a self-signed or expired certificate is now refused instead of crawled.

### Speech

#### Speech (STT/TTS)

| Version | Profile | License |
|---|---|---|
| Carried forward | `stts` | MIT |

**What's new in 2026.11**

- Every upgrade installs missing speech models, from the offline package where one is at hand, and reports by name the ones it could not install.
- Hermes and Moltis agents can use the box's speech services, and agent panes may use the microphone.

### Documents

#### <img src="/branding/media/razzfazz-ai_docling_icon.png" width="20" height="20"> Docling

| Version | Profile | License |
|---|---|---|
| `v1.32.0 → v1.38.2` | `docling` | MIT |

**What's new in 2026.11**

- The web UI at `/ui` is new, and its link to an external viewer is removed so no converted document is sent to a third-party site.
- The Docling MCP server runs behind the MCP gateway, with a bounded document cache (`DOCLING_MCP_CACHE_MAX_DOCUMENTS`).

**Security**

- Docling no longer writes the Valkey password into its log, and every log copy the box makes masks credentials inside addresses.
- The URL check behind the gateway refuses a name when any of its addresses is private.

---

#### <img src="/branding/media/razzfazz-ai_pdf_icon.png" width="20" height="20"> Stirling-PDF

| Version | Profile | License |
|---|---|---|
| `2.14.3 → 3.3.0` | `stirling-pdf` | MIT |

**What's new in 2026.11**

- Stirling-PDF 3 brings a text editor, better form editing, automatic rotation, improved redaction and viewing of password-protected PDFs.
- Sign-in stays with the box's single sign-on, and workflows that call the PDF tools directly keep working.

**Migration**

- Settings are converted at first start, so going back means restoring the pre-upgrade backup.

---

#### Document Conversion (Gotenberg)

| Version | Profile | License |
|---|---|---|
| `8.37.0 → 8.40.0` | `gotenberg` | MIT |

**What's new in 2026.11**

- Chromium's connections to Google's push service are refused inside the box.

**Security**

- Every Chromium conversion runs in its own browser context, so a cookie from one request no longer authenticates another.
- 8.40.0 fixes an unauthenticated file read and stops the PDF renderer from trusting the compromised eMudhra root certificates.
- Remote downloads, webhooks and office conversions to internal addresses are refused with `403`; upload the file instead.

**Migration**

- The request body limit is counted on the raw body, so a request just under a configured limit can now be rejected.

---

#### <img src="/branding/media/razzfazz-ai_documents_icon.png" width="20" height="20"> Paperless-ngx

| Version | Profile | License |
|---|---|---|
| `2.20.15 → 3.4.2` | `paperless-ngx` | GPL-3.0 |

**What's new in 2026.11**

- Paperless-ngx 3 adds relative-date custom-field searches and custom fields in workflow titles.
- The Paperless setup now also runs on every upgrade and `rzfz post-install --refresh`.

**Security**

- CVE-2025-14009 and CVE-2025-64459, which upstream fixed only in the 3.x line, are closed, as is a permissions issue with shared documents.

**Migration**

- The first start migrates the database, drops the task history and rebuilds the search index, which takes several minutes on a large archive.
- The upgrade is one-way, and `rzfz upgrade` never lowers the image.
- A PDF that already has a text layer no longer gets a second archive copy unless `PAPERLESS_ARCHIVE_FILE_GENERATION=always` is set in a compose override.
- Saved views that searched notes or custom fields without a field name need `notes.note:` or `custom_fields.value:`.

---

#### Presidio (PII Detection)

| Version | Profile | License |
|---|---|---|
| `2.2.362 → 2.2.366` | `presidio` | MIT |

**What's new in 2026.11**

- Presidio's analyzer and anonymizer ship smaller images from the project's new registry; the image redactor is unchanged.

**Security**

- Known vulnerabilities in its dependencies are closed.

### Git

#### <img src="/branding/media/razzfazz-ai_git_icon.png" width="20" height="20"> Git (Gitea)

| Version | Profile | License |
|---|---|---|
| `1.27.3 → 28.4.0` | `gitea` | MIT |

Sub-images bumped together with the main image:

| Sub-image | Version |
|---|---|
| gitea-mcp | New in 2026.11 (`1.7.0`) |

**What's new in 2026.11**

- Gitea 28 adds an audit log, bot accounts, deploy tokens and live notifications.
- Gitea can serve as a knowledge base for agents and Chat through the MCP gateway, with per-user connections and branch-only writes.
- Technical service users give automations a restricted, owner-named identity with scoped tokens.
- The access log is on by default and the webhook delivery timeout is a stack setting.
- Gitea takes its time zone from the stack's `TZ` setting, so it starts after a host upgrade to Ubuntu 26.04.

**Security**

- 28.4.0 fixes remote code execution through repository creation by a signed-in user and readable password hashes, and limits failed two-factor attempts per account.
- The administrator set-up keeps the password off the process list.

**Migration**

- Gitea 28 migrates its database at first start, so going back means restoring the pre-upgrade backup.
- Completed Actions runs older than 400 days are deleted nightly unless `RUN_RETENTION_DAYS = 0` is set in `app.ini` before the upgrade.
- Hand-edited webhook or migration allow-lists no longer accept `*` or `external`.

### Knowledge

#### <img src="/branding/media/razzfazz-ai_cognee_icon.png" width="20" height="20"> Cognee (GraphRAG)

| Version | Profile | License |
|---|---|---|
| `1.5.3 → 1.6.5` | `cognee` | Apache-2.0 |

Sub-images:

| Sub-image | Version |
|---|---|
| cognee-frontend | `v1.5.3 → v1.6.5` |
| cognee-mcp | `1.5.3.dev1` (carried forward) |

**What's new in 2026.11**

- Cognee limits concurrent model calls (`COGNEE_LLM_MAX_CONCURRENT`), so larger document sets build a knowledge graph without errors.
- Parallel searches no longer fail on a locked dataset, and a busy dataset answers `503` with `Retry-After` and no server path.
- The brain overview answers in pages, so a person with hundreds of datasets no longer stalls Cognee for everyone.
- Deleting a dataset stops its running graph build and removes the answers cached from it.
- A watchdog restarts a Cognee that stopped answering (`COGNEE_LIVENESS=0` turns it off).
- One runaway answer is capped at 16,384 tokens instead of blocking a model slot for an hour.
- Personal Cognee tools find knowledge shared with the person, renew their sign-in, and the company-brain tools answer from company knowledge only.

**Security**

- Cognee's admin account has its own generated password instead of the Authentik admin password.
- Cognee's sign-in tokens are bound to the account's password.

**Migration**

- Everyone signs in to Cognee again once.
- Cognee migrates its relational schema forward at start, and its graph keeps its on-disk format.
- Documents added while the configured chat model was not served were never processed and need to be added again.

---

#### <img src="/branding/media/razzfazz-ai_rag_icon.png" width="20" height="20"> LightRAG

| Version | Profile | License |
|---|---|---|
| Carried forward | `lightrag` | MIT |

**What's new in 2026.11**

- The upgrade and `rzfz post-install --refresh` point LightRAG at a chat model the box serves, and a CPU box's installation uses the embedding model it runs.

### Agents

#### MCP & Agent Manager

| Version | Profile | License |
|---|---|---|
| Rebuilt during the cycle; production | `agents` | Proprietary |

Agent images bumped with the module:

| Agent image | Version |
|---|---|
| Hermes | `v2026.8.27 → v2026.9.24` |
| Moltis | `20260827.01 → 20261023.01` |
| opencode (coding agent) | `1.18.25 → 1.18.32` |
| OpenHands | Removed |

**What's new in 2026.11**

- The MCP & Agent Manager is a production module.
- Agent ownership is decided by the user id first.
- An "Update available" badge, automatic updates of idle agents and `rzfz agents update` move agents onto the current image.
- The agent pages, the pane layout and the coding-agent terminal work on phones and foldables.
- Terminal tabs and their start commands come back once each after a restart, also after an unplanned one.
- The file panel shows upload progress and results, streams files up to 200 MiB, and gives uploaded files to the agent's user.
- Coding agents default to a coding model the fleet actually serves and prefer a GPU-class model over the CPU default, and Codex can edit files with a local model and knows its full context size.
- Turning the module off stops the users' agents, and turning it on again starts the ones that were running.
- Saving an agent type in the Settings portal keeps it enabled.
- Agents can be launched with a personal or a technical identity, checked before anything is created.
- Regenerable caches are left out of an agent's backup, so a home folder over the size limit only because of caches is backed up again.

**Security**

- No agent holds the host's Docker socket.
- Agents read their owner's OpenBao secrets through a read-only proxy, with a token on an owner-only tmpfs that is revoked with the owner.

**Migration**

- OpenHands is switched off and removed with its images and sign-in application, while its data volumes are kept and listed by the upgrade.
- A personal OpenHands agent stays listed, stopped, until its owner deletes it.

---

#### MCP Integrations (part of MCP & Agent Manager)

| Version | Profile | License |
|---|---|---|
| Rebuilt during the cycle; production | `mcp` | Proprietary |

**What's new in 2026.11**

- The MCP gateway checks the caller, uses the person's own credential and writes an audit entry with the person's Authentik id for every call.
- A group marked as a knowledge scope is recognised by its flag, not by its name.
- A Google Workspace connection asks for offline access, so it no longer ends after an hour.
- Cloud-only MCP servers are hidden on an offline box with the reason.
- A client protocol error from an MCP server (for example listing tools before initialising) passes through instead of becoming a `502`.

**Security**

- When Open WebUI signs in through Authentik (the default), chat tools act as the person by their verified Authentik sign-in, so changing a user's e-mail address in Open WebUI no longer reaches another person's connections.

**Migration**

- A personal connection saved before 2026.11 that is not tied to the person's account id is connected again once, and the person's MCP page says so.
- Users who connected Google before this release connect it once more.

---

#### Jira & Confluence (MCP gateway)

| Version | Profile | License |
|---|---|---|
| New in 2026.11 (`mcp-atlassian 0.23.1`) | `mcp-atlassian` | MIT |

**What's new in 2026.11**

- Jira and Confluence are reachable from agents and Chat through the MCP gateway, with a per-user OAuth connection and service-account credentials for technical users, experimental and off by default.

---

#### Microsoft 365 (MCP gateway, read-only)

| Version | Profile | License |
|---|---|---|
| New in 2026.11 (`ms-365-mcp-server 0.156.2`) | `mcp-m365` | MIT |

**What's new in 2026.11**

- Microsoft 365 is readable from agents and Chat through the MCP gateway with each person's own connection, experimental and off by default.

---

#### <img src="/branding/media/razzfazz-ai_paperclip_icon.png" width="20" height="20"> Paperclip

| Version | Profile | License |
|---|---|---|
| Carried forward | `paperclip` | MIT |

**What's new in 2026.11**

- Paperclip instances get distinct database names, and the image is built from a pinned base image.

### Communication

#### <img src="/branding/media/razzfazz-ai_matrix_icon.png" width="20" height="20"> Matrix (Synapse + Element)

| Version | Profile | License |
|---|---|---|
| Synapse `v1.158.0 → v1.164.2` | `matrix` | AGPL-3.0 |

Sub-images bumped together with the main image:

| Sub-image | Version |
|---|---|
| Element Web | `v1.12.27 → v1.12.33` |

**What's new in 2026.11**

- The Matrix server name is fixed once in `MATRIX_SERVER_NAME`, so a domain change no longer breaks Synapse.
- Matrix and Element get their certificates as soon as the module is switched on.
- Element X apps can refresh their sign-in again.

**Security**

- A sign-in that differs from an existing account only in letter case is refused instead of taking that account over.
- Element Web no longer lets a new login skip device verification by default and closes a script-injection hole in the HTML chat export.

**Migration**

- The upgrade fills `MATRIX_SERVER_NAME` from the existing Matrix data, and it must not be changed on a box with Matrix users.

### Security

#### <img src="/branding/media/razzfazz-ai_vault_icon.png" width="20" height="20"> OpenBao

| Version | Profile | License |
|---|---|---|
| New in 2026.11 (`2.6.3`) | `openbao` | MPL-2.0 |

**What's new in 2026.11**

- A secrets store with Authentik single sign-on, catalogue roles as policies and per-user and team paths, experimental and off by default.
- When the module is on, the MCP manager keeps the OAuth tokens of personal and technical tool connections in OpenBao; access tokens and API keys stay encrypted in its database.
- Its snapshot and encrypted keys are part of the backup, a restore checks that the restored store unseals and its credentials log in, and `rzfz status` shows the snapshot's age.
- A tool compares Infisical with OpenBao and moves secrets across, and a guide in the Help Center walks through it.

**Security**

- The seal refuses to re-key or re-initialise over existing data.
- Its audit log is decoded and alerted on by Wazuh.

---

#### <img src="/branding/media/razzfazz-ai_secrets_icon.png" width="20" height="20"> Infisical

| Version | Profile | License |
|---|---|---|
| Carried forward, deprecated | `infisical` | MIT |

**What's new in 2026.11**

- Infisical is deprecated: it keeps working, the Settings portal warns when you enable it, and it will be removed in a later release.

---

#### <img src="/branding/media/razzfazz-ai_vault_icon.png" width="20" height="20"> Vaultwarden

| Version | Profile | License |
|---|---|---|
| `1.37.2 → 1.37.5` | `vaultwarden` | AGPL-3.0 |

**What's new in 2026.11**

- Live sync works again, so the web vault and the apps receive changes immediately.
- Vaultwarden's mail follows the SMTP page of the Settings portal, and with mail off a new user can set a first master password.
- The upgrade's check for existing vaults before a database move fails closed instead of skipping the move.
- Version 1.37.5 is needed by current Bitwarden web vaults to switch two-step login on or off.

**Security**

- Seven advisories fixed in 1.37.4 are closed, the most serious in the removal of organisation members, and 1.37.5 fixes two more in website icons and emergency access.

**Migration**

- Rotate an organisation's API key after the upgrade if you do not fully trust that organisation's admins, because earlier versions let them read it.

---

#### <img src="/branding/media/razzfazz-ai_wazuh_icon.png" width="20" height="20"> Wazuh

| Version | Profile | License |
|---|---|---|
| `4.14.7 → 4.14.10` | `wazuh` | GPL-2.0 AND Apache-2.0 |

**What's new in 2026.11**

- Agent registrations survive a recreate of the manager.
- The stack's own audit log, Authentik administrator events, Gitea access logs and OpenBao's audit log have their own rules.
- The event store can be left out of the nightly backup with `BACKUP_INCLUDE_WAZUH_INDEX=false`.
- The guide describes the sign-in you really get: the box login, then the dashboard's own `admin` login in the default mode.

**Security**

- Wazuh 4.14.10 closes a heap write in the rule engine, restricts agent upgrade commands, closes idle enrolment connections and fixes several API access-control rules.

**Migration**

- Run `scripts/install-wazuh-agent.sh` again after the upgrade to move host agents to 4.14.10.

### Enterprise Search

#### <img src="/branding/media/razzfazz-ai_search_icon.png" width="20" height="20"> Onyx (Enterprise Search)

| Version | Profile | License |
|---|---|---|
| `v4.3.9 → v4.9.4` | `onyx` | MIT |

**What's new in 2026.11**

- Onyx indexes documents and answers chat with the correct embedding address and its own LLM Manager key.
- The Onyx setup runs on every upgrade, waits for a model that is still loading and keeps one search index.
- A domain ending in `.local`, `.test` or a similar reserved suffix no longer stops installation at Onyx.

**Security**

- Upstream's dependency security pins of the 4.4 to 4.9 lines are included.

**Migration**

- The first start runs the database migrations of the 4.4 to 4.9 lines, so going back means restoring the pre-upgrade backup.
- Onyx stays on its Vespa index, no re-index is needed, and existing sources are indexed once more after the upgrade.

---

## Operator UX: Settings portal and apps

### Settings portal (`settings.<domain>`)

- Switching a module on or off runs as a job with its step, elapsed time, the service it waits for and a "Cancel this change" button.
- A module change that waits too long on an unhealthy service ends as failed, names the service and switches the module off again.
- A second change during a running one shows what is running and opens its live output.
- The portal reconnects to its configuration by itself when a tool on the host replaced `.env`.
- Allowed Google Workspace domains are a list under Authentication, and an empty list admits nobody.
- The time source is set under Offline / Network.
- The Chat page shows Open WebUI's tool and code-interpreter settings.
- The Security page lists every security document the box carries and draws the architecture diagram offline.
- Knowledge scopes show who can read each scope.
- The update check uses a read-only token instead of the git credentials mount.
- The dashboard keeps showing the running containers when an overlay file cannot be read, and names that file.

### Start portal (`start.<domain>`)

- Link shortcuts accept https targets (http only on the box's own domain), checked on the server, and every user can keep personal links.
- Agent-app shortcuts can open the person's agent with a repository and start command pre-filled, which the person confirms.

### LLM Console (`llm.<domain>`)

- Deployments show their workers and instance states in a layout that stays readable on phones.
- The Placement panel explains each placement and lets an administrator prefer or pin a worker.
- Each model instance shows the engine build it runs.
- Every worker shows its configured and available VRAM, and deployments and cached models show their exact origin where it is recorded: the Hugging Face repository with a link, the files, the quantisation and the size.
- Vision projector files are no longer offered as a model quantisation.
- The deploy dialog shows the model's licence, with a link to the licence text where the repository names one, and a link to the model's Hugging Face page; a licence the repository does not state is flagged.

### Agent Manager (`agents.<domain>`)

- A phone shows one pane with a compact, scrollable tab row, and terminal tabs can be renamed by long-press.
- The terminal stays above the on-screen keyboard, and renames and closes use in-page dialogs that a browser cannot block.

### Help Center (`help.<domain>`)

- The curated module pages for Wazuh, OpenUEM and Open WebUI open first, with the upstream mirror one link away.
- The Dify documentation is mirrored for offline use, and new guides cover the time source, Infisical to OpenBao, Codex CLI, technical MCP users, Start portal links and Gitea as a knowledge base.
- A Known issues page per release line lists each issue with its workaround and the release that fixes it.

### Licenses & Attribution (`license.<domain>`)

- Components appear under their product names instead of container names.

### Command line

- `rzfz status` gains rows for knowledge scopes, group sync, the metadata block, the time source, worker release lines, OpenLIT's default login and the factory-reset password.
- `rzfz agents update`, `rzfz module` (Matrix and module data resets), `rzfz node-upgrade` and `rzfz relaunch-engines` are new.
- `rzfz status` warns about a compose overlay that lies outside the stack directory.
- The handover documents describe the box they come with: one row per application, a first sign-in note and the installed models.

---

## Fixed since 2026.09-ga.5

Defects of earlier releases that 2026.11 fixes, each listed with its workaround
on the Known issues pages of the Help Center.

- Administrator multi-factor sign-in, once switched on, is enforced and asks for enrolment.
- Enabling Microsoft Entra or Google sign-in in the Settings portal adds the login button.
- Rotating the backup password, or changing the admin password, no longer makes earlier backups unrestorable.
- `rzfz upgrade` stops when the pre-upgrade backup fails.
- Once the backup disk was full, every later nightly backup failed too; the backup now clears space or stops with the numbers.
- A nightly backup produces its archive even when preparing the agent data takes long.
- A single-volume backup can be restored with the single-volume restore, which no longer restarts the whole stack.
- A full restore reports a failed volume or `.env` as a failure, and personal agents and model engines start afterwards.
- A full restore no longer stops on a symlink or on a database name with a hyphen.
- An upgrade retried at the same version pulls images again.
- `rzfz upgrade --rollback` restores the code on a box upgraded from an offline package.
- Rotating the cache password no longer leaves Dify's background workers disconnected.
- `rzfz set-admin-password` without `--password` sets the password you type.
- A generated admin password without a digit no longer stops Dify setup.
- Cognee no longer asks for a missing secret when you enable it later.
- After an upgrade, Cognee and LightRAG use a chat model the box serves.
- A CPU box with about 32 GB of memory can embed.
- A freshly installed box is hardened at installation.
- After a domain change on the command line, the Settings portal keeps the new domain.
- On a box whose domain ends in `.local`, Onyx and the services after it are set up, and Onyx indexes documents.
- Enabling Matrix in the Settings portal no longer leaves Synapse restarting.
- Gitea and Vaultwarden start after the certificate bundle was a directory.
- Gitea starts after the host is upgraded to Ubuntu 26.04.
- Dify knowledge bases on an offline box finish indexing.
- Vaultwarden with mail off accepts a new user's first master password.
- Saving an agent type in the Settings portal no longer disables it.
- Creating several model replicas at once is no longer refused when the fleet has room.
- The LLM Manager's registry cleanup no longer deletes the images remote workers pull.
- A backup whose preparation ran longer than ten minutes no longer reports success with an incomplete archive.
- After a factory reset, Dify, the Settings portal and the knowledge services reach the models again: service keys the LLM Manager no longer accepts are issued anew.
- After a factory reset, the nightly automatic image cleanup of the monitoring module stays off.
- A short network hiccup inside the box no longer shows as an error page: the reverse proxy tries a failed connection again for up to five seconds and never repeats a request a service already received (the optional legacy GPUStack module may repeat a read request).

---

## Upgrade instructions

### From `2026.09-ga.5` to `2026.11-ga`

This is a supported one-step upgrade. Run it from the stack directory:

```bash
rzfz upgrade --target 2026.11-ga
```

The upgrade takes a pre-upgrade backup, updates the code, applies the **153
environment migrations** recorded for 2026.11, rebuilds or pulls images as
needed, restarts the stack, runs the post-install refresh and ends with a health
check.

Before you start on `2026.09-ga.5`:

1. Undo every local fix, so that `git status --short | grep -v '^??'` prints nothing.
2. Take a verified backup by hand. On 2026.08 and 2026.09, the backup the upgrade takes first can be incomplete when preparing it runs longer than 10 minutes (many or large agent volumes), and it reports success anyway; the upgrade also continues when that backup fails. From the stack directory:

   ```bash
   git fetch --tags origin
   git show 2026.11-ga:scripts/verified-backup-before-upgrade.sh > verified-backup-before-upgrade.sh
   bash verified-backup-before-upgrade.sh
   ```

   Go on only when it ends with `VERIFIED COMPLETE`. If it lists agent volumes as left out, copy them separately or accept that they are not in the backup (nightly backups apply the same size limit). The upgrade still takes its own backup; that one is an extra, not the one to rely on.
3. The upgrade re-seals the factory-reset record when it no longer matches the admin password; if it reports that it could not, run `rzfz set-admin-password --all`.

After the upgrade:

1. Update each thin worker with `rzfz node-upgrade`; `rzfz upgrade` lists the ones that are behind. A worker installed before 2026.11 does not have the command yet: fetch it once from `https://<master>/worker-bundle/node-upgrade.sh` and run it with `--master` (see the command reference).
2. Run `rzfz agents update --dry-run` to see which personal agents still run the previous image.
3. On a box that ran OpenHands with Gitea, delete the Gitea access token named `openhands` of the Gitea administrator account.
4. Run `scripts/install-wazuh-agent.sh` again on hosts with a Wazuh agent.
5. Take a new full backup.

### Upgrade notes

- **Release channel:** customer boxes run the public release channel: set `RAZZFAZZ_CHANNEL=public` in the stack `.env` before the upgrade. From 2026.11 on, a customer box left on the internal channel is refused an online upgrade.
- **Moving to the public channel:** that upgrade moves the box to the public mirror and, after the upgrade is verified, removes all development history from the box's git checkout, including branches and tags whose newest commit carries no development files, saving any stashes as patches under `~/.razzfazz/stash-patches/` first.
- **Valkey password:** the upgrade replaces `VALKEY_PASSWORD` once, recreates Valkey and every service that uses it (among them Authentik, the backup service, Gitea, Dify, Onyx, Paperless, Infisical, Paperclip, Docling, the LLM Manager and its router, the Agent Manager and, with observability on, the Valkey exporter) and re-keys running personal agents, so expect a short interruption.
- **Copies of the Valkey password:** a script or password-safe entry that holds the old value needs the new one.
- **Update check:** the Settings portal's update check needs `STACK_UPDATE_TOKEN` set to a token with the `read:repository` scope only, where the repository does not allow anonymous reads; `STACK_GIT_CREDENTIALS_PATH` is retired.
- **One-way steps:** paperless-ngx, Onyx, Gitea, Stirling-PDF, Synapse, Vaultwarden, Cognee and, with observability on, OpenLIT and ClickHouse migrate their data, and the upgrade never lowers them.
- **Build time:** the upgrade rebuilds every image the stack builds itself (among them Caddy, Cognee, Dify, backup, the Settings portal, the agents, the worker agent and the LLM Manager), which takes longer than a pull.
- **Admin MFA:** a box that enforces administrator multi-factor sign-in keeps it, and a new installation starts with it off until the handover.
- **Google sign-in:** `GOOGLE_OAUTH_DOMAIN` keeps the domains an installed box has, and a new installation with Google sign-in must name its domains. The upgrade and `rzfz status` warn when the value is still the domain an earlier template shipped as its default; set your own Google Workspace domains in the Settings portal (Authentication), or turn Google sign-in off there. The value is never changed automatically.
- **Ollama-compatible endpoint:** nothing in the stack uses it by default. A client you pointed at it (for example an Ollama connection in Open WebUI) sends `Authorization: Bearer <OLLAMA_PROXY_CLIENT_KEY>` (generated by the upgrade) to `ollama-proxy:11434` on the stack network; a tool on the box host that used `127.0.0.1:11434` moves to the LLM Manager endpoint `https://llm.<your-domain>/v1` with its own API key, which also meters its use.
- **Model registry:** pushing to the registry's local port by hand needs the registry write credential the upgrade generates (`LLM_REGISTRY_WRITE_PASSWORD`): `docker login 127.0.0.1:8093 --username llm-manager --password-stdin`. Pulling works as before.
- **Database accounts:** the upgrade creates one database account per service, hands each service its existing data and stops before any restart if a new account cannot log in.
- **LLM engines:** the upgrade relaunches the box's engines at its end, so a model can be briefly unavailable; `--no-engine-relaunch` leaves them as they are and `rzfz relaunch-engines` moves them later.
- **Microsoft sign-in:** a first Microsoft sign-in links to an existing account only when the app registration sends the `xms_edov` claim, as the Entra sign-in guide describes.
- **Personal agents created before 2026.11:** recreate each one once with Update (or Reinstall) in the MCP & Agent Manager so it picks up the box's own certificate authority; stopping and starting it is not enough. Until then an agent on a box with its own TLS certificates cannot reach its memory and tools on the box.
- **Compose overlays outside the stack directory:** an entry of `COMPOSE_FILE` that points outside the stack directory still applies to the stack, but the Settings portal cannot read it and `rzfz status` warns about it; move such an overlay into the stack directory and point `COMPOSE_FILE` at its new place.
- **Mac workers:** a Mac worker installed earlier keeps its Ollama on port 11434 until `ollama-port-migrate.sh` moves them, and a Mac worker that already serves models needs them deployed once more.

### AMD Strix Halo host preparation

The host operating system is upgraded separately from the stack with
`scripts/razzfazz-upgrade-os.sh` (Ubuntu 24.04 → 26.04: `--check`, the upgrade
with its reboot, then `--reconcile`). The legacy `razzfazz-host-upgrade.sh`
kernel and ROCm tool is disabled and is not needed on Ubuntu 26.04.
Take a snapshot first, and re-enable Secure Boot after the upgrade.

### Boxes on a pre-reorg release

A box still on **2026.04, 2026.05 or any 2026.06-ga.x** first crosses the 2026.07
reorg with `razzfazz-upgrade-from-2026.04-GA.x.sh`, then upgrades normally.

### Incremental upgrades within the cycle

A box on a 2026.11 release candidate upgrades to `2026.11-ga` with `rzfz upgrade`;
the environment migrations it already applied are not applied again.

```bash
rzfz upgrade                # to the newest release on the box's channel
rzfz upgrade --check        # dry run: show what would change
```


### Rollback

```bash
rzfz upgrade --rollback
```

Restores the code and the `.env` files from before the upgrade and recreates the
stack; it does not restore data. A service whose data the newer release has
already migrated (for example paperless-ngx, Onyx, Synapse, Vaultwarden,
Stirling-PDF, Gitea or Dify) keeps its newer version and is named, so the
previous image never starts over migrated data. To go back including data,
restore the pre-upgrade backup: `rzfz backup restore <backup>` (`rzfz backup list`
lists them).

---

## Security

### Image version changes with security content

| Image | Before | After | Driver |
|---|---|---|---|
| LiteLLM (router, Mac gateway) | `main-v1.83.7-stable` | `v1.108.4` | Host-header authentication bypass CVE-2026-49468; security fixes for file uploads |
| paperless-ngx | `2.20.15` | `3.4.2` | CVE-2025-14009, CVE-2025-64459, shared-document permissions |
| Crawl4AI | `0.9.3` | `0.9.5` | Nine advisories in request handling |
| Vaultwarden | `1.37.2` | `1.37.5` | Seven advisories plus two in icons and emergency access |
| Open WebUI | `0.11.3` | `0.12.4` | Access-control and account-safety fixes of 0.12.0 to 0.12.4 |
| Caddy | `2.11.4` | `2.11.7` | Path canonicalisation, upstream connection reuse |
| Gotenberg | `8.37.0` | `8.40.0` | Per-conversion browser context, body limit before parsing; unauthenticated file read (GHSA-r77c-ghhj-665p), distrust of the compromised eMudhra roots (GHSA-cfqv-cv4g-hw85) |
| Wazuh | `4.14.7` | `4.14.10` | Rule-engine heap write, API access-control rules |
| Dify plugin daemon | `0.6.10-local` | `0.6.12-local` | Go toolchain and library findings, archive extraction |
| Element Web | `v1.12.27` | `v1.12.33` | HTML-export script injection, device verification |
| Komodo | `2.3.3` | `2.3.4` | Withdrawn certificate-authority root |
| Valkey | `9.1.2` | `9.1.3` | Upstream security fixes |
| Presidio | `2.2.362` | `2.2.366` | Dependency vulnerabilities |
| Synapse | `v1.158.0` | `v1.164.2` | Memory exhaustion through the public room list (msgpack) |
| Stirling-PDF | `2.14.3` | `3.3.0` | Request-size bounds, mobile scanner relay |
| Gitea | `1.27.3` | `28.4.0` | Authenticated remote code execution through repository creation (CVE-2026-125423), readable password hashes (CVE-2026-123006) |

The image-level CVE delta against `2026.09-ga.5` is computed at the GA cut by the
release security review (SBOM plus CVE diff), whose gate blocks the tag on a new
critical or high finding.

### Security-relevant changes in behaviour

- Personal agents now trust the box's own certificate authority in addition to the public ones, so they accept the box's internal certificates; an agent created earlier needs to be recreated once with Update or Reinstall.
- Technical users can be given the company's MCP integrations; each binding is set by an administrator and acts with that technical user's rights.
- The reverse proxy tries a failed connection to a service again for up to five seconds; it never repeats a request the service already received (the optional legacy GPUStack module may repeat a read request).
- Customer boxes upgrade from the public release channel, and switching removes the vendor's development history from the box's checkout.

### Findings closed this cycle

- The host-hardening script no longer leaves the Docker socket world-writable.
- Personal Moltis agents no longer receive the host's Docker socket.
- On a thin LLM worker, a model engine can no longer reach the worker's Docker API once the worker is updated with `rzfz node-upgrade`.
- Document conversion no longer writes the Valkey password into its log, and the upgrade rotates that password once.
- A Microsoft or Google sign-in can no longer take over an existing account through an unverified e-mail address.
- A Matrix sign-in that differs only in letter case can no longer take over an existing account.
- An Open WebUI administrator who changes a user's e-mail address can no longer reach another person's tool connections through the MCP gateway.
- A new installation with Google sign-in no longer admits one fixed company domain by default.
- Open WebUI no longer holds the Cognee administrator password, and Cognee's admin no longer shares the Authentik admin password.
- The published default logins of OpenLIT and Cognee are replaced or locked when their module is enabled.
- Administrator multi-factor sign-in, when switched on, is enforced.
- A fresh installation is hardened before the stack starts; where that needs a password the installer cannot ask for, its final summary names the command to run.
- A crafted backup archive can no longer write outside the restore folder.
- Containers on the stack host and on thin LLM workers can no longer reach cloud metadata addresses; installing or upgrading a worker installs the block, or names the command to run when it cannot.
- An unused, unfiltered outbound route to the Dify plugin marketplace is removed.
- Every service connects to the database with its own account, which can reach only that service's data, so installed Dify plugins and the Dify sandbox no longer hold the database administrator credential. The upgrade creates the new accounts, hands each service its existing data and checks every account before any service restarts.
- The monitoring module's own database has its own password, so the monitoring services no longer hold the main database's administrator password; the change is applied automatically when the module starts.
- The model registry is read-only for anonymous clients: services on the stack network and the local registry port can still download models, only the LLM Manager can add, replace or delete them, and remote workers keep their sign-on.
- The Ollama-compatible endpoint requires a key and is no longer published on the box host.
- The document-conversion service refuses internal addresses for remote downloads, webhooks and office conversions; uploading a file for conversion works as before.
- The monitoring database's debugging endpoint is reachable only inside its own container.
- Model engines that still ran with an older runner or older container settings are relaunched by the upgrade, and `rzfz status` checks the running engines instead of the setting.
- The Gitea administrator set-up and the observability database's clients keep their passwords off the process list.
- The upgrade and `rzfz status` warn when Google sign-in still admits the domain an earlier template shipped as its default.

### Open and accepted residuals

- The Help Center's page capturer is a third-party program with known findings in a bundled library, and is rebuilt with the fixed library in a later release.
- The Docker socket proxy carries a known vulnerability without an upstream fix, and on the master it is reachable only from the control plane's internal network and from the host itself, never from the LAN; on a thin worker the model engines no longer reach it once the worker is updated with `rzfz node-upgrade`.
- Open WebUI forwards user details to every OpenAI-compatible connection, including external providers, unless forwarding is switched off in the Settings portal.
- The optional legacy GPUStack module keeps an unfixed SAML authentication flaw, stays off unless you enable it, and its module description states this where you enable it.
- Cognee's MCP server image remains on a vendor pre-release, because its publisher has released no newer image.
- Personal agents reach the internet freely by default, and `AGENT_EGRESS_POLICY=allowlist` together with its compose overlay (see the Agents module guide) limits them to named hosts.
- Some upstream images still carry Critical findings in bundled packages, at the same level as in 2026.09, and they are updated as their publishers release fixed images.

---

## Known issues at GA

The known issues of this release line, each with its workaround and the release
that fixes it, are on the **Known issues in 2026.11** page in your box's Help
Center (`https://help.<your-domain>/`, Known issues).

---

## For engineers

Chronological detail for this cycle is in [`changelog.md`](changelog.md); the
30-second summary is in [`WHATS_NEW.md`](WHATS_NEW.md). The notes of the
individual release candidates are kept under `releases/2026.11-rc*/`.
