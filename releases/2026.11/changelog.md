# Changelog — 2026.11

Engineering view, one paragraph per release candidate. The customer-facing
narrative is in [`RELEASE_NOTES.md`](RELEASE_NOTES.md); the 30-second summary is
in [`WHATS_NEW.md`](WHATS_NEW.md).

**Scope of the cycle (at 2026.11-rc28, the GA candidate):** about 2,620 commits since
`2026.09-ga.5`, 133 environment migrations recorded under the 2026.11 candidates,
three new profiles (`openbao`, `mcp-atlassian`, `mcp-m365`), one removed profile
(`openhands`), 31 moved pins in the manifest's image lists, and new upstream
bases for the Caddy, Cognee, backup and coding-agent builds. There was no 2026.10
GA; its work is part of this cycle.

## 2026.11-rc1 (2026-09-29, internal, not tagged)

The hardening candidate. Each application service received only the environment
keys it reads, most credentials left command lines and logs, agents lost the host's Docker
socket, the Caddy admin API closed to user-code containers, and Dify's outbound
proxy started refusing every special-purpose address range and the box's own
addresses. The LLM Manager gained exact admission and placement, a 16 GB CPU box
got an embedding model that fits, the MCP & Agent Manager and its MCP
integrations became production modules, Infisical was deprecated, and a long list
of upgrade, backup, restore and offline defects of 2026.09 was fixed.

## 2026.11-rc2 (2026-10-23, internal, not tagged)

The upstream sweep: every patch and minor update of the bundled services, Cognee
1.6, Open WebUI 0.12.3, Dify 1.18.0 with plugin daemon 0.6.12, and four larger
steps (paperless-ngx 3, Onyx 4.9, Caddy 2.11.7, LiteLLM v1.106.2). Admin MFA
enforcement became off by default so a box can be handed over, and the Mac
worker's managed Ollama moved to its own port.

## 2026.11-rc3

The first candidate whose notes carry everything since `2026.09-ga.5`. It added
the fix for command-line domain changes reaching the Settings portal, setup on
domains with reserved suffixes such as `.local`, and Matrix and agent setup on
an offline `rzfz init`.

## 2026.11-rc4 (2026-10-26)

Cognee's graph database extension was installed where the service reads it,
Onyx started without a shell and indexed with its own key, helper tools stopped
passing secrets as arguments, and `rzfz upgrade` and `rzfz init` gained a final
health check. Vaultwarden's database move started to fail closed, a Mac worker
learned to remember its models across a restart, and switching back online
refused while the offline firewall was active.

## 2026.11-rc5 (2026-10-30)

The Onyx and Paperless setup moved into every upgrade, GB10 and DGX workers
joined from an amd64 master through a multi-architecture worker agent, and
`node-upgrade` began verifying the new agent through its API. The time source
became a setting, the Settings portal replaced the default logins of OpenLIT and
Cognee, the factory-reset record started following the admin password, and the
installation stick began protecting attached disks.

## 2026.11-rc6 (2026-11-02)

Dify knowledge bases indexed on offline boxes, allowed Google domains became a
list, Open WebUI tool servers followed the MCP module, Vaultwarden live sync
worked again, and every self-built image moved to a base pinned by digest. The
final health check judged only active services, workers enrolled under the old
manager name stayed connected, and Valkey, Gotenberg, Open WebUI 0.12.4, Synapse,
LiteLLM, docling-serve, the backup service and Moltis were updated.

## 2026.11-rc7 (2026-11-09)

The largest candidate of the cycle. The engines moved to llama.cpp v0.9.0, the
LLM Manager gained worker liveness, token ceilings, `503` with `Retry-After`,
placement explanations and the Responses API, and containers were blocked from
cloud metadata addresses. Knowledge scopes started reconciling hourly, Chat
tools began acting as the person by their Authentik sign-in, a Microsoft or
Google sign-in stopped linking through an unverified e-mail, and Cognee gained
paging, a liveness watchdog and safer deletes. The Valkey password was rotated
once, observability moved to OpenLIT 2.1 on ClickHouse 26.3, Gitea moved to 28,
Stirling-PDF to 3, Wazuh to 4.14.10, OpenHands was removed, and the Agent
Manager's file panel and terminal were reworked.

## 2026.11-rc8 (2026-11-10)

The console showed workers and instance states per deployment, multi-token
prediction became configurable in the model editor, Hermes moved to v0.21.5,
engine instance names became valid host names, and LiteLLM moved to the v1.108.4
long-term-support line. The agent portal and terminal were reworked for phones,
and agent caches left the backup.

## 2026.11-rc9 (2026-11-11)

A generation stops when the application that asked has given up, `rzfz status`
confirms the factory-reset password, an upgrade keeps an enforced admin MFA, the
Cognee liveness watchdog can be switched off, and the agent portal shows which
agents still run an older image.

## 2026.11-rc10 (2026-11-12)

`rzfz agents update` and the "Update available" badge completed the agent
rollout after an upgrade, and token reports recorded an estimate for abandoned
requests instead of zero.

## 2026.11-rc11 (2026-11-12)

The scheduled backup began waiting for a running upgrade, and an upgrade for a
running backup. The installation stick checked disk space before loading images
and loaded only the images for the box's own hardware.

## 2026.11-rc12 (2026-11-12)

`sudo rzfz status` read the installing account's image-build record, a factory
reset run from a script ended with success, and the upgrade lifted its pause on
the scheduled backup at the end.

## 2026.11-rc13 (2026-11-12)

A fresh CPU box wired Cognee and LightRAG to the embedding model it runs, and
`rzfz status` stopped expecting GPU images on a CPU stick install.

## 2026.11-rc14 (2026-11-13)

The Cognee web interface stopped checking for npm updates.

## 2026.11-rc15 (2026-11-13)

The installation stick carried the host-hardening packages, so fail2ban and
audit logging are active offline, and the sign-in service started on the first
try after a factory reset.

## 2026.11-rc16 (2026-11-13)

Models served by a worker node kept their structured-output, image-input and
thinking settings in Dify across upgrades, and workflows with a yes/no field in
their structured output ran again.

## 2026.11-rc17 (2026-11-13)

The Dify model connector moved to a version that declares structured output,
Dify stopped auto-updating the connectors the stack pins, and a new check runs
one structured-output request through Dify after provisioning and in
`rzfz status`.

## 2026.11-rc18 (2026-11-14)

The host's update-notification tool was switched off while a box is offline.

## 2026.11-rc19 (2026-11-15)

Coding agents restored their terminal sessions from the newest complete save
after an unplanned restart, and `RAZZFAZZ_AUTO_REBOOT=false` kept the automatic
reboot after security updates off.

## 2026.11-rc20 (2026-11-15)

Caddy's memory limit became configurable with `CADDY_MEM_LIMIT`, defaulting to
2 GB, with a warning in `rzfz status` near the limit.

## 2026.11-rc21 (2026-11-15)

The installation stick asked before wiping an existing installation, installed
the newest kernel, and offline packages stopped carrying internal design
documents.

## 2026.11-rc22 (2026-11-16)

The installer itself switched off the update-notification tool on stick
installs, and the stick laid down a single release snapshot without development
history.

## 2026.11-rc23 (2026-11-16)

A stick install started on first boot again with the help and start-portal
images built without internal design documents, and the update-notification
tool was switched off before its first hourly run.

## 2026.11-rc24 (2026-11-16)

The LLM Console showed each worker's VRAM and each model's exact origin, customer
boxes moved to the public release channel with local development history removed,
the Settings dashboard kept its container list when an overlay could not be read
and `rzfz status` warned about overlays outside the stack directory, a backup that
began before its preparation step finished was never marked complete, personal
agents trusted the box's own certificate authority, and technical users could be
given the company's MCP integrations.

## 2026.11-rc25 (2026-11-17)

After a factory reset, `rzfz post-install` issued new service keys where the LLM
Manager no longer accepted the old ones and kept the monitoring module's nightly
image cleanup switched off; the upgrade notes gained the verified backup step for
2026.08 and 2026.09 boxes.

## 2026.11-rc26 (2026-11-17)

The reverse proxy retried a failed connection to a service for up to five seconds
without repeating a received request (the legacy GPUStack routes may repeat a read request), the verified backup helper worked on
real 2026.08 and 2026.09 boxes, and the switch to the public channel removed the
complete development history quickly on large histories.

## 2026.11-rc27 (2026-11-19)

Every service connected to the database with its own account and Komodo's database got its own password,
the model registry became read-only for anonymous clients, the Ollama-compatible endpoint required a key
and left the host port, Gotenberg refused internal targets and FerretDB's debug listener moved to loopback.
The upgrade relaunched engines onto the release runner and `rzfz status` measured them, a rollback kept the
newer image of a service whose data had moved forward, thin workers got the metadata block, the Gitea
administrator set-up and the ClickHouse clients kept their passwords off the process list, and Gitea moved
to 28.4.0 and Gotenberg to 8.40.0.

## 2026.11-rc28 (2026-11-20), the GA candidate

Komodo started again on upgraded boxes, because its database address was built in compose instead of read from
`.env`, and the dead `KOMODO_DATABASE_URI` key was removed. The Ollama-compatible endpoint's health check sent
its own key, so the endpoint became healthy and the upgrade's final health check passed, and the Gotenberg
documentation in the Help Center started at its real first page.

## 2026.11-ga

Cut from 2026.11-rc28 with no code change. The release notes, the Known issues
pages (four entries added for issues accepted for this release), the security
documentation and the stack reference for consultants' agents were brought to
the final state of the cycle.
