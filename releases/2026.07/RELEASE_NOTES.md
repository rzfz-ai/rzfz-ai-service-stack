# Release Notes — 2026.07 cycle

**Status:** General Availability · **Latest:** v2026.07-ga.10 (2026-07-19) ·
**GA:** v2026.07-ga (2026-07-06) · **Prior cycle:** 2026.06-ga.7

The 2026.07 cycle reached General Availability as **v2026.07-ga**, consolidating
two release candidates (rc1, rc2). It is the largest cycle since the platform's
first GA: the product is renamed and rebranded to **the rzfz.ai Stack**, published
under an **open-core licensing model**, delivered to customers through a public
**Codeberg** mirror, reorganised behind a single **`rzfz`** command, and given a
per-user coding-agent and Model-Context-Protocol (MCP) workspace platform. Dify
moves to 1.15.0. Per-tag detail lives in `releases/2026.07-rc1/`,
`releases/2026.07-rc2/`, `releases/2026.07-ga/`, and `releases/2026.07-ga.1/`.

**Upgrade paths validated for GA** (test box): the standard next-cycle path plus,
critically, the **pre-reorg big-bang crossing**. Because 2026.07 reorganises the
repository, **any** box on a pre-reorg release — 2026.04-ga.x, 2026.05-ga.x, *and*
2026.06-ga.x — must upgrade into 2026.07 through the reorg-aware bootstrap
`razzfazz-upgrade-from-2026.04-GA.x.sh` (operator decision B; #113/#128), not the
plain upgrade. The GA gate caught and fixed the final upgrade-path and provisioning
defects before ship — the pre-reorg bootstrap seeding, a `rzfz upgrade --check`
abort on absent optional keys, and a per-user image-builder no-op.

**v2026.07-ga.1 (2026-07-07) — day-1 functional-readiness patch.** The GA
acceptance suite validated install, health and SSO but not real user journeys;
hands-on functional testing of the GA box surfaced a cluster of day-1 defects, all
fixed in ga.1: Open WebUI listed zero models, the default chat model returned an
empty answer, several custom-build modules would not enable on a restricted-egress
box, some Help-Center mirrors were incomplete, and a few Configuration-Portal panels
errored — and `rzfz upgrade` did not carry the day-1 provisioning fixes forward.
ga.1 makes both a fresh install and an upgrade reach the same working, chat-ready
state (upgrade now auto-runs the day-1 `--refresh` self-heal and deploys any missing
default models), and adds a **Day-1 user-journey test tier** (`tests/day1/`) that
deploys a model, logs in, chats and clicks each module UI on a live box — now a
release gate alongside install / health / SSO. Within the cycle ga.1 is the ordinary
`rzfz upgrade` and adds no new `.env` keys. (day-1 Issues A–J; #147, #148)

**Patch history (ga.2 → ga.9).** ga.2 (2026-07-08) fixed the built-in module
documentation, restored the default chat model's reasoning, and hardened the public
Codeberg export. ga.3 (2026-07-09) fixed an OIDC/SSO login regression on Let's
Encrypt boxes — the OIDC trust bundle is now a superset of public + Caddy-internal +
operator CAs, so login verifies across every TLS mode (#152). ga.4 (2026-07-10)
reconciled three production hotfixes into source: upgrades no longer abort when a
single container was already unhealthy (#153), per-service database table ownership
is repaired automatically so migrations don't crash with "must be owner" (#154), and
onyx-vespa gets 8 GB of headroom (#154); it is also the first release whose public
Codeberg upgrade path (`RAZZFAZZ_CHANNEL=public`, anonymous curated-mirror pull) was
validated end-to-end on the reference box before the tag. ga.5 (2026-07-10) is the
one defect that public-path validation caught: on a `RAZZFAZZ_CHANNEL=public` box an
upgrade could crash the Configuration Portal because the curated public `.gitignore`
dropped the governance checksum DB (so `git stash --include-untracked` stashed it
away and Docker recreated its mount as a directory); the curated ignore list now
matches the internal one, and internal/fleet boxes were never affected (#156). ga.6
(2026-07-10) completes that fix: because the upgrade's stash runs from the *source*
box's `.gitignore`, a box already on an older affected export still crashed on the
transition, so `restart_stack` now coerces the checksum DB back to a regular file
before recreating containers — the upgrade self-heals the Config Portal from any
earlier build (verified on the reference box). ga.7 (2026-07-11) hardens the upgrade
*driver* after the prod rollout exposed three ways it could stumble: a divergent old
git tag aborting the fetch, the pre-upgrade backup silently skipping when `rzfz`
isn't on PATH, and the pre-upgrade auto-stash accumulating (#158). ga.8 (2026-07-11)
is a **critical** fix: automated and upgrade backups were failing *silently* on any
box with a live database (offen raced the write-ahead log and wrote no file), now
fixed by briefly quiescing PostgreSQL during the archive — with the restore path
unchanged (#157). ga.9 (2026-07-11) is a public-delivery polish: the Configuration
Portal's Release Notes / What's New panels were blank on Codeberg installs because the
public export culled `releases/` wholesale, so the export now ships the consolidated
cycle docs the Config UI reads — the combined GA + patch view — while the internal
per-tag directories (SBOM, security assessments, test results) stay excluded (#159).
**Customer/public boxes should target ga.9** — it's the first export whose in-product
release notes populate. All eight patches are the ordinary within-cycle `rzfz upgrade`
with no new `.env` keys.

## Cross-cutting themes

1. **Open-core licensing + public Codeberg delivery** *(rc1, finalised at ga)* —
   the stack is published under a clear three-way model: a free **Community** tier
   (Apache-2.0), a source-available tier (Business Source License 1.1) governed by
   the **rzfz.ai Subscription** with free private/evaluation use and a rolling
   Change Date (2029-07-08 for this release), and bundled upstream components under
   their own licences. New root `LICENSE` / `LICENSE-APACHE` / `LICENSE-BSL` /
   `NOTICE` / `THIRD-PARTY-NOTICES.md`, a plain-language `docs/LICENSING.md`, and a
   `stack.yaml`-driven per-component map rendered live at `license.<domain>`.
   Customer boxes set `RAZZFAZZ_CHANNEL=public` and pull from the public Codeberg
   mirror (the upgrade pre-flight redirects `origin` there automatically); a
   hardened export ships only the customer-needed surface and a Community Wiki is
   published alongside. (#26, #101, #105, #27, #28, #123, #124, #127)
2. **The `rzfz` unified CLI + repository reorganisation** *(rc1)* — every
   management script is consolidated behind a single `rzfz` command, and the
   repository is reorganised into `modules/`, `core/`, `cli/`, and `config/`. The
   `setup.<domain>` web wizard is retired: first-run is `rzfz init`, dangerous
   operations are `rzfz setup`, and RAG-model selection and TLS-certificate upload
   move into the Configuration Portal. The `COMPOSE_FILE` LLM-overlay path is
   migrated automatically on upgrade. The whole product is rebranded razzfazz.ai →
   rzfz.ai. (#26, #34, #22, #35)
3. **Box-local Enterprise overlay + delivery channel** *(ga)* — the Help Center
   serves the open community documentation by default and runtime-mounts a gated
   Enterprise documentation overlay when one is present; the USB build and
   `rzfz package` deliver that overlay into `overlay/enterprise/` so an entitled
   box carries the extended material without it being published to the public
   mirror. The `RAZZFAZZ_CHANNEL` (internal → git.razzfazz.ai, public → Codeberg)
   model draws the line between the two channels. (#125, #126, #129)
4. **Coding-agent split + per-user MCP & shared company memory** *(rc1)* — the
   personal coding agents become per-type sandboxed containers (opencode, Codex,
   and a user-defined slot), each isolated, with a first-party Hermes v0.18
   dashboard, a live web-app port preview, and three-layer agent-memory governance.
   Each user gets their own MCP proxies with an encrypted credential vault and a
   two-tier Cognee "memory" (a private per-user brain and an admin-managed shared
   company brain) wired into the agents — all under one unified **MCP & Agent
   Manager**. A central password broker fans a new password out to Authentik, Dify
   and Cognee in one step. (#84, #36, #88, #61, #54)
5. **Dify 1.15 + true SSO + version currency** *(rc1)* — Dify is de-vendored (the
   custom web frontend builds from a clean upstream clone) and bumped 1.14.2 →
   1.15.0, with 24 database migrations and the plugin-auto-upgrade backfill wired
   to run automatically. True SSO adds group-to-role mapping for the four
   natively-integrated apps (Open WebUI, Gitea, Dify, Cognee), auto-seeds their
   native OIDC credentials, and drops the redundant double login. A CVE/currency
   sweep moves Open WebUI, Gitea, SearXNG, Crawl4AI, ClickHouse, LightRAG,
   Cognee (1.2.2) and the legacy `gpustack:vulkan` base to current, patched
   releases. (#26, #107, #33, #18, #20, #79)

## Release candidates (rc1 → rc2 → ga)

- **rc1** *(2026-07-04)* — consolidated the entire cycle's development into one cut
  (the crossing applies all cycle env deltas at once): the open-core licensing
  reorganisation, the coding-agent split + unified MCP & Agent Manager, Dify 1.15,
  true SSO, the `rzfz` CLI + repository reorganisation, the rebrand, and the
  public-delivery mechanism. A box crossing from 2026.06-ga.7 receives **52
  auto-applied env deltas** (2 breaking, both auto-migrated).
- **rc2** *(2026-07-05)* — rc1 plus three fixes: the Onyx v4 clean install
  (`onyx_user` `CREATEROLE`; #111), the pre-reorg big-bang upgrade bootstrap seed
  (#113), and the finalised licensing naming — the **rzfz.ai Subscription** (tier)
  and **the rzfz.ai Stack** (product) (#110). No new `.env` keys over rc1.
- **ga** *(2026-07-06)* — rc2 plus the box-local Enterprise documentation overlay
  and its USB/`rzfz package` delivery (#125/#126/#129), the Community Wiki publish
  and export hardening (#123/#124/#127), and the final fixes: the pre-reorg big-bang
  bootstrap made the single supported crossing for every 2026.04/05/06-ga.x box
  (operator decision B; #113/#128), the `rzfz upgrade --check` abort on absent
  Paperclip/Matrix keys (#131), the Moltis image-builder no-op (#133), and the
  init-test fixture/timing updates (#131/#132). No new `.env` keys over rc2. Ships
  with the full Mode-A security assessment.

## Module versions

The cycle ran a full version-currency sweep. Notable third-party moves against the
prior cycle: Dify 1.14.2 → 1.15.0 and its plugin-daemon 0.6.1-local → 0.6.3-local,
Open WebUI 0.9.5 → 0.10.2 (past the known-bad 0.9.6), Gitea 1.26.2 → 1.26.4
(CVE-2026-20896 admin auth-bypass, network-mitigated on our stack, bumped for
defence-in-depth), Crawl4AI 0.8.9 → 0.9.0 (two Critical RCEs), ClickHouse
24.8.14.39 → 25.8.25.37 (EOL LTS carrying four unpatched bundled-OpenSSL CVEs →
supported LTS), SearXNG 2026.6.13 → 2026.7.3, LightRAG v1.4.16 → v1.5.4, Cognee
→ 1.2.2, plus the offen backup image pinned immutable (v2.48.2) and the Paperclip /
Hermes / Moltis custom-image pins reconciled. dify-sandbox 0.2.15 and the GPUStack
runtimes were intentionally held. The full pin set is `config/manifests/versions.json`;
per-tag CVE rationale is in the `2026.07-rc1` migration-manifest note.

## Security posture

- The cycle's SSRF surfaces are hardened: the MCP manager constrains OAuth/proxy
  targets to HTTPS with a host allow-list (#67), and Dify's access to internal
  document tools is a scoped allow-list rather than a blanket grant (#23). The
  coding-agent workspace routes are source-IP anchored (#68). The public-export
  mechanism fails closed on secret detection and culls the delivered surface to
  customer-need-only (#27/#124/#91).
- **GA full Mode-A compliance audit (NIS2 / ISO 27001:2022 / OWASP LLM Top 10):**
  performed against a clean reference install. Verdict **STRONG, cleared for
  release, no blocker**; no active-compromise indicators, external and internal
  active scans returned zero matched findings, and no reachable Critical/High.
  Report: `security-run/razzfazz-ai-box-security-assessment-v2026.07-ga.md`.
- A per-image CycloneDX SBOM and a CVE diff versus the previous GA (v2026.06-ga.7)
  are committed under `releases/2026.07-ga/sbom/` (supply-chain evidence,
  R-DEF-01 / OWASP-LLM-03).

## Validated at GA

- The pre-reorg big-bang upgrade crossing (2026.04 / 2026.05 / 2026.06-ga.x →
  2026.07) through the reorg-aware bootstrap, and the standard next-cycle path.
- Both customer-facing security artifacts refreshed for GA: the as-built security
  architecture document (`docs/security-architecture.md`) and the audit-grade
  Mode-A assessment report.

---
*Issued by razzfazz.ai GmbH — Member of SEQIS Group. For support, use your razzfazz.ai support contact.*
