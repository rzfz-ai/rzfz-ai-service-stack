# Changelog — 2026.06 cycle

Prior cycle: 2026.05-ga.7. Per-tag detail in `releases/2026.06-rc1/` …
`releases/2026.06-rc4/`, `releases/2026.06-ga/`, `releases/2026.06-ga.1/` …
`releases/2026.06-ga.5/`.

## v2026.06-ga.7 (2026-06-22) — docs + SSO rate-limit patch
- Help Center: cached Dify/OpenLIT (Mintlify/Next.js) doc assets resolve to the
  cache root — fixes the unstyled-page rendering; own_docs-policy documented.
- Configuration Portal: post-enable acceptance-smoke note softened (no false
  "probe FAILED" alarm; the enable was never affected).
- Caddy F-B7: Authentik auth rate-limit scoped to the credential-entry flow
  (`default-authentication-flow` executor) and raised to 30/min/IP — fixes false
  HTTP 429 on legitimate multi-app SSO while preserving brute-force protection.
- Getting Started (guide + PDF): first-login points at the razzfazz.ai Portal
  (`start.<domain>`); PDF Backup section starts on its own page. PDFs regenerated
  against the ga.7 doc set.
- No image/version bumps; no `.env` changes; install path untouched. The
  Authentik `email_verified` mapping fix was deferred to 2026.07 (blueprint did
  not deploy on upgrade).

## v2026.06-ga.6 (2026-06-22) — clean-install hardening + PSA PDF→JSON
- Clean-install fixes (post-install py_script quoting, model-skip on re-run,
  Dify default embedding → qwen3-embedding, account-timezone, mcp_sse pin,
  gpustack plugin compatibility_mode); harden-host live-stack guard.
- OpenWebUI document/RAG + websearch provisioned via post-install; reboot + 3h
  soak validated. No new HIGH security findings (Mode-A).

## v2026.06-ga.5 (2026-06-21) — customer-handover + repo-hygiene patch
- backup-encryption: set BACKUP_ENCRYPTION_PASSWORD at fresh init (was empty →
  backups refused); rotate-bootstrap-password.sh handles the empty case
- new razzfazz-security-check.sh (customer-runnable posture+CVE)
- factory-reset-to-delivery-state (immutable encrypted delivery-credentials blob
  + manual fallback)
- rewritten §16 day-1 handover checklist (all 8 per-app admins, no-spare-host
  restore drill, signup-closed verify, ufw expected output, recovery model)
- scripts/docs/security-run READMEs; removed .continue/.roo; usb-appliance →
  tools/appliance/; commit-msg full-release-name hook
- full Mode-A assessment (0 active-scan findings, 0 new Critical); no image bumps

## v2026.06-ga.4 (2026-06-20) — upgrade-path hotfix
- #184 v2: reconcile_service_db_users probes per-service DB role auth before
  healing an empty *_DB_USER; leaves it empty (global-superuser fallback) when
  the role can't authenticate. Fixes the ga.3 regression that broke
  authentik/openwebui/dify on older docker-superuser boxes (hit on the 0.208
  ga.2→ga.3 upgrade)
- generate-sbom.sh: cve-diff keyed on (cve,severity) not image-SHA (ga.2→ga.3
  NEW 7528→775) + hard-fail on empty syft output
- host-script-only; no image rebuild, no env changes, no third-party bumps;
  no security-relevant surface (ga.3 Mode-A assessment carries forward)

## v2026.06-ga.3 (2026-06-20) — bug-fix + RAG-quality patch
- upgrade self-heals empty per-service `*_DB_USER` from `.env.example` (fixes
  onyx/paperless "password authentication failed for user docker" crash-loop
  after upgrade); regression originated in a 2026.05-rc2 migration entry
- upgrade reclaims ownership of root-owned container files before `git stash`
  (stops the stash-abort; also un-breaks docling German OCR via the corrected
  tessdata bind-path)
- qwen3-embedding is the fleet-standard embedding (dim 2560, 32K ctx) for
  cognee/lightrag/openhands + OpenWebUI RAG, replacing nomic's 2048-tok cap;
  not auto-flipped on upgrade (post-install + pgvector reset required on
  cognee/lightrag boxes, OWUI RAG reindex)
- razzfazz-status treats the stable `llm-cpu` profile correctly (no longer
  flagged as legacy)
- unattended USB-appliance installer (Ubuntu 26.04 autoinstall + first-boot
  stack-prep)
- security-arch generator emits the §14 NIS2 + ISO 27001 tables and §8 admin-MFA;
  compliance test suite now green
- full Mode-A security assessment on a clean reference install (0 active-scan
  findings, no reachable Critical/High); no third-party image bumps
  (`requires_build` true, `requires_pull` false)

## v2026.06-ga.2 (2026-06-18) — bug-fix patch
- per-user agent terminals reconnect after a reboot (agent-manager
  re-registers each instance's Caddy route on boot; previously "Websocket not
  connected" until delete+redeploy)
- Help Center GPUStack docs are version-correct + scoped to the active LLM
  profile; Config UI "Stack documentation" opens the Help Center by redirect
  (styling/links/identity all resolve)
- cognee / LightRAG knowledge-graph building works on the default reasoning
  model again (thinking disabled via the correct `chat_template_kwargs` path)
- single-box GPU no longer double-counted in the hardware dashboard
- coding-tools / paperclip build the tool versions shipped in the release on
  upgrade (build-pin force-sync; ends the stale opencode/gsd drift)
- new optional `.env` key `BACKUP_TMP_DIR` (+ internal `backup-tmp` volume): on
  small-OS-disk boxes, point the backup's temp build at the dedicated backup
  disk so it can't fill the OS disk
- no image bumps; `requires_build` true, `requires_pull` false

## v2026.06-ga.1 (2026-06-15) — docs-only
- refreshes this cycle's "What's New" to GA framing (drops the stale
  "release-candidate phase" footer that shipped in v2026.06-ga)
- no code/env/image/compose/auth surface vs ga; `requires_build`/`pull` false
- note: the Dify reset-password follow-up flagged at ga is resolved upstream in
  the shipped Dify 1.14.2 image (verified on prod) — nothing to reconcile

## v2026.06-ga (2026-06-14) — General Availability
- GA of the 2026.06 cycle (qwen3.6 single default @1M + RAG defaults + version sweep)
- upgrade paths validated both ways on the test box: 2026.05-ga.7→ga (standard) and
  2026.04-ga→ga (big-bang bootstrap); 3 upgrade-path bugs caught + fixed pre-ship:
  authentik-media first-mount race; bootstrap lib*.sh seeding; OpenWebUI legacy
  peewee migratehistory reconcile (no more chat.share_id crash-loop on 04-ga→ga)
- Mode-A security review PASS (external clean; CVE flat-vs-ga.7, none release-introduced)
- no new .env keys; requires_build + requires_pull; follow-ups: Dify reset-pw patch (ga.1)

## v2026.06-rc4 (2026-06-14)
- version-currency sweep (no open CVE forced any): gotenberg 8.34.0, openlit
  1.22.0, searxng 2026.6.13, synapse v1.154.0, element-web v1.12.21, infisical
  v0.161.0, vespa 8.703.17, tika 3.3.1.0, docling-serve-cpu v1.23.0 (validated)
- custom agent rebuilds: coding-tools gsd-pi 3.0.0 + opencode 1.17.5 (stale
  fallbacks fixed), paperclip v2026.609.0
- held: authentik 2026.5.x, onyx v4, openhands 1.8.0, hermes-agent v2026.6.5
  (#161), gpustack 2.2.0; no new .env keys; requires_pull + requires_build

## v2026.06-rc3 (2026-06-13)
- qwen3.6 the single default for all roles (chat/general/coding/vision) at
  ctx=1M/parallel=4; gemma4 + qwen3-coder-next pre-downloaded then scaled to 0
- RAG product defaults seeded into Open WebUI (reranker URL+key, md-splitter off,
  chunk 5000/500, query-gen off, top_k 10)
- stale `gemma4` defaults across Dify/Onyx/config-UI/agent-manager re-pointed to
  the active default; config-UI dashboard no longer 500s on empty resource monitor
- validated by full clean install on the test box (16/16); no image bumps;
  VERSION/manifest → 2026.06-rc3

## v2026.06-rc2 (2026-06-12)
- #47 admin-password tool; #149 secret-rotation tool + danger matrix; #150 backup
  exclude-dify-plugins setting
- caddy/chrony clock-jump survival; #129 docling RQ engine; #136 onyx pool caps;
  authentik mem 2g; #142 upgrade auto-logzip; #131 init-suite assertion fixes
- no image bumps; VERSION/manifest → 2026.06-rc2

## v2026.06-rc1 (2026-06-09)
- #142 initiative: structured upgrade journal + diagnose-gate (#143), host-side
  outpost reconcile (#145/#147), Authentik session flush (#148), standalone log
  collection (#141), build gate (#133)
- backup/field: lightrag AGE-orphan fix (#134), SMTP transition warning (#140),
  agent-manager Super Admins (#138), ga.5-acceptance batch (#119)
- 11-image security/drift bump (ClickHouse + Crawl4AI CVEs, Dify 1.14.2, Gitea
  1.26.2, Authentik 2026.2.4); VERSION/manifest → 2026.06-rc1
