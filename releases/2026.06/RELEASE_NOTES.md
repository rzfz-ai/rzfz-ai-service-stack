# Release Notes — 2026.06 cycle

**Status:** General Availability · **Release:** v2026.06-ga (2026-06-14) ·
**Latest patch:** v2026.06-ga.6 (2026-06-21) · **Prior cycle:** 2026.05-ga.7

The 2026.06 cycle reached General Availability as **v2026.06-ga**, consolidating
four release candidates, and has since received six maintenance patches
(ga.1 – ga.6). Per-tag detail lives in `releases/2026.06-rc1/` …
`releases/2026.06-rc4/`, `releases/2026.06-ga/`, and `releases/2026.06-ga.1/` …
`releases/2026.06-ga.6/`.

**Upgrade paths validated for GA** (test box, both directions): 2026.05-ga.7 →
2026.06-ga (standard) and 2026.04-ga → 2026.06-ga (big-bang bootstrap). The GA
gate caught + fixed three upgrade-path defects before ship — a clean-install
volume race, the big-bang bootstrap's shared-library seeding, and an OpenWebUI
schema-migration that crash-looped on a direct upgrade from the oldest baseline
(now reconciled automatically).

## Cross-cutting themes

1. **Upgrade observability & self-healing** *(rc1)* — a structured per-run upgrade
   journal with an automatic diagnose-gate, plus auto-remediation of the top
   recurring field failures (outpost bindings, stale Authentik sessions, init
   tools on restricted networks). Upgrades are now diagnosable and self-correcting
   instead of failing silently.
2. **Credential management** *(rc2)* — a tool to set any app admin password
   (`razzfazz-set-admin-password.sh`) and a class-enforced infrastructure-secret
   rotation tool with a danger matrix (`razzfazz-rotate-secret.sh` +
   `docs/secret-rotation-danger-matrix.md`). Secrets are classified SAFE /
   COORDINATED / DATA-LOSS / DO-NOT-ROTATE and the tooling enforces the class.
3. **Clock-jump survival** *(rc2)* — chrony `makestep` so a virtualized-clock skew
   can't expire the internal TLS leaf and take down every gated UI.
4. **Concurrency & resource correctness** *(rc2)* — docling RQ engine (UI works
   with multiple workers), Onyx connection-pool caps, authentik-server memory bump.
5. **Backup control** *(rc1+rc2)* — LightRAG AGE-orphan backup-completeness fix
   (rc1) and an optional Dify-plugin-data exclusion setting (rc2).
6. **One default model + grounded RAG** *(rc3)* — qwen3.6 becomes the single
   default for every role (chat/general/coding/vision) at 1M context, the optional
   models are pre-downloaded then stopped (0 replicas), and the reranker/chunking/
   query defaults that make document Q&A find the right facts ship as product
   defaults.
7. **Version-currency sweep** *(rc4)* — nine third-party images and two custom
   agent images moved to current releases ahead of GA; no open CVE forced any, all
   targets verified and all custom builds re-compiled.

## Patch releases (ga.1 → ga.6)

After GA the cycle received six maintenance patches, all upgrade-path and
reliability focused — **no third-party image versions changed across the patch
line**:

- **ga.1** *(2026-06-15, docs-only)* — release-notes / documentation refresh.
- **ga.2** *(2026-06-18)* — upgrade-path self-healing: empty per-service secrets
  are regenerated on upgrade, and several reliability fixes for direct upgrades
  from older baselines.
- **ga.3** *(2026-06-20)* — further upgrade hardening and a knowledge/RAG quality
  improvement:
  - The upgrade now self-heals empty per-service **database users**, fixing a
    crash-loop on the document-management and enterprise-search modules after an
    upgrade.
  - The upgrade no longer aborts when a container has written root-owned files
    into the stack directory.
  - **qwen3-embedding** becomes the fleet-standard embedding model (≈2560-dim,
    32K context) for the knowledge-graph, RAG, agent and chat document-RAG paths,
    replacing the previous model whose 2048-token limit could not embed full-size
    document chunks.
  - A new **USB-appliance installer** performs an unattended operating-system
    install and prepares the stack for first boot.
  - The status tool no longer mis-reports the stable CPU LLM profile as a legacy
    profile.
  - The customer security documentation now emits the full machine-checkable
    NIS2 and ISO 27001 control-mapping tables.
- **ga.4** *(2026-06-20)* — upgrade-path hotfix for older installations:
  - The ga.3 database-login self-heal is now **conditional** — it switches a
    service to its dedicated database login only after verifying that login
    works, and otherwise preserves the working shared administrator login. This
    fixes a regression where ga.3 could take Authentik, Open WebUI and Dify
    offline on older boxes that share a single database administrator account.
  - Recommended over ga.3 for the upgrade path; host-script only, no image or
    setting changes.

- **ga.5** *(2026-06-21)* — customer-handover + repo-hygiene release:
  - Fresh installs now set the backup-encryption passphrase correctly (a bug left
    it empty → the backup service refused); password rotation handles the empty case.
  - New `razzfazz-security-check.sh` — a self-service posture + CVE check the
    customer can run any time (also run per-box post-install).
  - Factory reset now returns the box to delivery state (the sticker password).
  - The day-1 handover checklist is rewritten to be fully actionable on a single
    box (rotate all admin accounts, no-spare-host restore drill, signup-closed
    verification, firewall expected output, recovery model).
  - No third-party image version changes; full Mode-A security assessment included.

- **ga.6** *(2026-06-21)* — clean-install hardening + document-extraction enablement:
  - Fresh AMD/Strix-Halo installs load models again (host Vulkan userspace installed
    by init); gpustack-legacy v0.7.1 Vulkan builds reproducibly from source, and a
    build-gate verifies the runner actually runs.
  - Dify PDF→JSON path completed: qwen3.6 + qwen3-embedding defaults, gpustack plugin
    0.0.15 with thinking-param passthrough, the internal-AI-tools SSRF allow-list made
    standard, and per-app concurrency backpressure so a workflow can't overrun the LLM.
  - OpenWebUI document/RAG + web search settings now persist (written via the OWUI API);
    Config Portal module toggles and Authentik login branding fixed on clean installs.
  - harden-host gained a live-stack guard (it had torn down a running stack); a clean
    ga.6 install and a reboot-survival test both passed on a Strix-Halo box.
  - No third-party image version changes; full Mode-A security assessment included.

## Module versions

rc1 ran the comprehensive security/CVE sweep (ClickHouse 24.8.14.39, Crawl4AI
0.8.9, Dify 1.14.2, Gitea 1.26.2, Authentik 2026.2.4, Valkey 9.1.0). rc2/rc3
changed no images. **rc4 is a freshness sweep** (no open CVE forced any): gotenberg
8.34.0, openlit 1.22.0, searxng 2026.6.13, Synapse v1.154.0, element-web v1.12.21,
infisical v0.161.0, vespa 8.703.17, tika 3.3.1.0, docling-serve-cpu v1.23.0; plus
custom agent builds coding-tools (gsd-pi 3.0.0 / opencode 1.17.5) and paperclip
v2026.609.0. Held to post-GA: Authentik 2026.5.x, onyx v4, OpenHands 1.8.0,
hermes-agent v2026.6.5, GPUStack 2.2.0. See `releases/2026.06-rc4/changelog.md`.

## Security posture

- rc1 shipped the ClickHouse + Crawl4AI CVE remediations and the Authentik
  2026.2.4 SAML-signature-wrapping patch.
- rc2 adds no new exposed surface: the new tools are operator-run CLI scripts, the
  new env key is a backup toggle.
- **ga.3 full Mode-A compliance audit (NIS2 / ISO 27001 / OWASP LLM Top 10):**
  performed against a clean reference install. No active-compromise indicators;
  external and internal active scans returned zero matched findings; no reachable
  Critical/High. The patch line ships no new container versions, so the CVE
  posture equals the GA-assessed baseline (residual volume is upstream "won't-fix"
  operating-system base-layer advisories, not live exposure). Full report:
  `security-run/razzfazz-ai-box-security-assessment-v2026.06-ga.3.md`.
- **ga.5 full Mode-A audit:** clean reference install; 0 active-scan findings;
  0 new Critical, 3 new High (upstream-DB drift on unchanged images). Report:
  `security-run/razzfazz-ai-box-security-assessment-v2026.06-ga.5.md`.

## Validated at GA and across the patch line

- Full clean-install suite (19/20 scenarios; the one miss is a known slow-CPU
  build-timing artefact, not a defect) and the standard upgrade path
  (previous-GA → 2026.06-ga.3) on the test box.
- Both customer-facing security artifacts refreshed: the as-built security
  architecture document and the audit-grade assessment report.

---
*Issued by razzfazz.ai GmbH - Member of SEQIS Group.*
