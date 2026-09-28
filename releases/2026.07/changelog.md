# Changelog — 2026.07 cycle

Prior cycle: 2026.06-ga.7. Per-tag detail in `releases/2026.07-rc1/`,
`releases/2026.07-rc2/`, `releases/2026.07-ga/`, `releases/2026.07-ga.1/`,
`releases/2026.07-ga.2/`, `releases/2026.07-ga.3/`, `releases/2026.07-ga.4/`,
`releases/2026.07-ga.5/`, `releases/2026.07-ga.6/`, `releases/2026.07-ga.7/`,
`releases/2026.07-ga.8/`, and `releases/2026.07-ga.9/`.

## v2026.07-ga.10 (2026-07-19) — reliability polish (post-install · Dify · backup · Onyx)
- Post-install no longer hangs on CPU-only boxes (#187).
- Published Dify web-apps reachable without the SSO gate (RZFZAI-1308); `rzfz upgrade`/`--refresh` initialises Dify (RZFZAI-1337).
- Nightly backup no longer stops postgres/postgres-komodo — physical datadirs excluded from the tar, captured logically via `pg_dumpall` (#182).
- Dify example knowledge-base seeding now runs during provisioning (#189).
- Onyx AI-search chat answers with citations — Qwen3 thinking-mode disabled for Onyx only (#170).
- Security: Authentik updated to 2026.2.6 (upstream High-severity advisory fixes).
- Docs: refreshed enterprise-documentation screenshots + a new TARIC classification tutorial; bilingual (EN/DE) website screenshots.

## v2026.07-ga.9 (2026-07-11) — Config-UI release notes on Codeberg installs (#159)
- The Configuration Portal's **Release Notes** and **What's New** panels were blank on
  Codeberg (public-channel) installs. The Config UI reads the consolidated cycle docs
  (`releases/<cycle>/{RELEASE_NOTES,WHATS_NEW}.md` — the combined GA + patch history),
  but the public export culled `releases/` wholesale, so both fell back to "No What's
  New notes available" on every customer box.
- Fix: `scripts/publish-public.sh` now ships **only** the three consolidated cycle docs;
  the internal per-tag directories (SBOM, security assessments, test results) stay
  culled, enforced by a new export HARD GATE 1a. Customer boxes now display the combined
  release notes.
- Also: corrected a stale `tests/test-sso-oidc.sh` check for `entra/14-flow-integration.yaml`
  (removed in ga.6's SSO unification into `z-15`); now asserts the unification. Test-only.
- No product/runtime/image change, no new `.env` keys — export tooling + a test.
  Mode-B + carry-forward; empty SBOM CVE-diff. **Customer/public boxes should target ga.9.**

## v2026.07-ga.8 (2026-07-11) — CRITICAL: fix silent backup failure (#157)
- Automated (nightly) AND upgrade-triggered backups were failing **silently** on any
  box with a live database — **no backup file written**. offen tarred the live
  postgres-data / postgres-komodo-data volumes while postgres recycled `pg_wal`
  segments mid-tar → `lstat …/pg_wal/…: no such file or directory` → the whole
  archive aborted. Prod had no automated backup for several days before it was caught.
- Fix: re-enable `docker-volume-backup.stop-during-backup=true` on both postgres
  containers. The pre-backup `pg_dumpall` runs first (db up), then offen stops
  postgres, tars a consistent quiescent data dir, and restarts it. **Restore path
  unchanged** → no DR risk. Brief postgres downtime during the 03:00 archive.
- The stop+start docker-DNS bug that removed this in 2026-05 (kernel 6.14 / Docker 28)
  is gone on the fleet's kernel 7.0.0-15 / Docker 29.1.3 (verified on 0.91).
- Two compose labels; no new `.env` keys, no image/upstream change. Mode-B +
  carry-forward; empty SBOM diff. **Every box should be on ga.8.**

## v2026.07-ga.7 (2026-07-11) — upgrade-path robustness (#158)
- Three fleet-wide `rzfz upgrade` driver fixes found during the prod ga.3→ga.6 rollout
  (none affected a running stack — they made the upgrade abort early or skip its backup):
  - `git fetch --tags --force` so a divergent OLD tag (e.g. a historically-moved
    `v2026.05-ga.4`) no longer aborts the upgrade when the target tag fetched fine.
  - Pre-upgrade full-stack backup calls `${SCRIPT_DIR}/rzfz` (PATH-independent) — no
    longer silently skips when `rzfz` isn't on PATH.
  - Pre-upgrade auto-stash of untracked runtime cruft is dropped after a successful
    checkout instead of accumulating (16 had piled up on prod); a tracked-edit stash
    is kept + flagged.
- `cli/upgrade.sh` only; no new `.env` keys, no image/upstream change. Mode-B + carry-
  forward; empty SBOM diff. Deferred: #157 (nightly offen backup pg_wal race).

## v2026.07-ga.6 (2026-07-10) — hotfix: complete the #156 public-channel fix (upgrade belt)
- Completes ga.5. ga.5's curated-`.gitignore` fix only protects go-forward (the
  upgrade's `git stash --include-untracked` runs from the source box's `.gitignore`),
  so a box already on an older buggy public export (ga…ga.4) still crashed the
  Config Portal when upgrading into the fix — confirmed on the reference box.
- `restart_stack` now coerces `.checksums.db` to a regular file before `docker
  compose up` (removes a stray directory; recreates the file). Runs from the target
  script post-re-exec → fixes the transition from any older export. Belt-and-
  suspenders with ga.5's `.gitignore` fix.
- Verified on 0.91: the box left broken by the ga.4→ga.5 transition self-recovered
  during the ga.5→ga.6 upgrade (Config Portal healthy). Internal boxes: no-op.
- No new `.env` keys; Mode-B (one host-script guard) + carry-forward; empty SBOM diff.
  **Customer/public boxes should target ga.6.**

## v2026.07-ga.5 (2026-07-10) — hotfix: public-channel Config-Portal crash (#156)
- Single-fix patch for a public-channel-only defect the ga.4 public-path test
  caught: after a `RAZZFAZZ_CHANNEL=public` upgrade, `razzfazz-config` could
  crash-loop (`sqlite3 unable to open database file`).
- The curated public `.gitignore` (publish-public.sh) omitted `.checksums.db`, so
  on a public box the upgrade's `git stash --include-untracked` stashed it away and
  `docker compose up` recreated its bind-mount source as a directory. Now ignored
  (matches internal boxes, which were never affected). Recovery for a bitten box:
  `docker rm -f razzfazz-config; rm -rf .checksums.db && touch .checksums.db;
  docker compose up -d --no-deps razzfazz-config`.
- Validated on the reference box (public ga.4→ga.5, Config Portal healthy). No new
  `.env` keys; Mode-B (one curated-.gitignore line) + carry-forward; empty SBOM diff.

## v2026.07-ga.4 (2026-07-10) — reliability: prod-hotfix reconciliation + public-path validation
- Reconciles three fixes applied live on production during the ga.3 rollout back
  into source so a later `rzfz upgrade` can't silently revert them, and hardens
  the upgrade path against a failure that aborted a real prod upgrade mid-flight.
- Upgrade robustness (#153): a pre-existing-unhealthy container (e.g. a red
  onyx-vespa) no longer makes `restart_stack`'s `docker compose up` abort the
  whole upgrade under `set -e` — the `RAZZFAZZ_VERSION` sync (Config-Portal
  version) and the post-upgrade `--refresh` self-heal now always complete.
- DB table ownership (#154): a new reconcile step reassigns each per-service
  database's public objects to the connecting role, fixing "must be owner of
  table" migration crashes (onyx-api / paperless) on boxes first provisioned as
  the global `docker` superuser. Idempotent; no-op on healthy boxes.
- onyx-vespa memory 4 GB → 8 GB (#154) for stability on populated indexes.
- Public Codeberg export hygiene (#151 follow-up / #155): cull dev-only `assets/`
  + nested QA `tests/` from the curated mirror (fail-closed gate). No shipped
  runtime asset removed — Authentik + start-portal icons intact.
- First release whose `RAZZFAZZ_CHANNEL=public` Codeberg upgrade path (anonymous
  pull of the curated mirror) was validated end-to-end on the reference box.
- No new `.env` keys over ga.3; ordinary `rzfz upgrade` within the cycle.
  Security: Mode-B + carry-forward (diff not security-relevant; ownership
  reconcile narrows privilege). No image change → empty SBOM CVE-diff.

## v2026.07-ga.3 (2026-07-09) — hotfix: OIDC/SSO trust-bundle regression
- Fixes OWUI/gitea/vaultwarden SSO login 500 on Let's Encrypt boxes (and self-signed
  boxes adding Google/Entra) — ga.2's caddy-internal-CA-ONLY SSL_CERT_FILE replaced the
  trust store so public issuers couldn't verify (#152). certs/caddy-ca.pem is now a
  SUPERSET (system public CAs + Caddy internal root + operator cert on TLS_MODE=certificate),
  rebuilt on fresh install AND rzfz upgrade. Root-caused + hotfixed live on prod 8.246.
- No image/port/service/config change (host CLI scripts only). Mode-B diff-review + carry-forward;
  empty SBOM CVE-diff.

## v2026.07-ga.2 (2026-07-08) — quality patch (Help-Center, model default, public export)
- From dogfooding the ga.1 upgrade on a rich demo box. Fixes the built-in module
  documentation, restores the default chat model's reasoning, and hardens the public
  Codeberg export.
- Help-Center (#149): 6 modules whose upstream docs are JS/CDN/SPA sites wget can't
  archive (Dify, Cognee, OpenHands, Gotenberg, Komodo, LightRAG) now serve box-local
  `module_docs` pages instead of broken mirrors; the paperless ReadTheDocs redirect
  is neutralised (+ regression test).
- qwen3.6 reasoning default (#150): reverted the ga.1 `enable_thinking=false` — thinking
  ON by default (disable per-request via OWUI/Dify); output-token budget confirmed
  adequate so reasoning doesn't truncate the answer.
- Codeberg export hardening (#151): strips the appliance/USB builder tooling (+ ISO
  backstop), SEQIS-internal MCP entries (moco/pipedrive), and dev asset cruft;
  rewrites manifest + internal repo/clone URLs to the Codeberg mirror; neutralises
  README enterprise-doc links — fail-closed export gates.
- No new `.env` keys over ga.1; ordinary `rzfz upgrade` within the cycle (Help image
  rebuilds). Carried: Cognee graph post-1.1.2→1.2.2 (re-cognify; data intact),
  Vaultwarden 1.36.0 SSO (#42). Security: Mode-B + carry-forward (export-hardening is
  security-positive; no image changes → empty SBOM diff).

## v2026.07-ga.1 (2026-07-07) — day-1 functional-readiness patch
- Makes a fresh install and an upgrade both reach a working, chat-ready box on day
  one. The GA acceptance suite validated install / health / SSO but not real user
  journeys; hands-on functional testing of the GA box surfaced a cluster of day-1
  defects, now fixed.
- Fresh-install day-1 fixes: Open WebUI 0-models (model-sync recreate after key-set;
  Issue B); qwen3.6 empty answer (`enable_thinking=false` in the backend params;
  Issue J); default model deployed first (Issue I); pre-build ALL custom-build module
  images so enable is instant + offline-safe (Issue A); Help-Center mirror fails
  honestly + strips the ReadTheDocs redirect (Issue C); Configuration-Portal
  What's-New 500 (Issue D), Security-Architecture nav 404 (Issue E), Komodo 400
  (Issue F), Crawl4AI 502 (Issue H).
- Upgrade self-heals to day-1-green: `rzfz upgrade` auto-runs the day-1 provisioning
  refresh (#147), the Authentik migration deadlock no longer aborts the upgrade
  `rc=1` (#147), and `--refresh` deploys any missing default models and waits for the
  default chat model (#148); Dify pgvector auth aligned (dataset indexing).
- Security handover (§16): Open WebUI native public signup defaults CLOSED (SSO
  unaffected); `rzfz post-install` leaves an as-built `rzfz security-check` posture
  report on every box; `security-check` reliability fixes; and the generated
  `docs/security-architecture.md` container inventory now covers the FULL stack (83
  services / all profiles) instead of only the always-on core.
- New Day-1 user-journey test tier (`tests/day1/`) — incl. a §16 security-posture
  probe — now gates every release alongside install / health / SSO (fresh 92/0;
  upgrade AFTER-green from 2026.04-ga and 2026.06-ga baselines).
- No new `.env` keys over v2026.07-ga; within the cycle this is the ordinary
  `rzfz upgrade`. Known issue: Vaultwarden 1.36.0 SSO email-verification (Issue G,
  deferred/#42, `xfail`). Security: Mode-B diff-review + carry-forward of the GA
  Mode-A assessment (STRONG, cleared); no image changes → empty SBOM CVE-diff.

## v2026.07-ga (2026-07-06) — General Availability
- Carries rc1 + rc2 forward and adds the final pre-GA round.
- Box-local Enterprise documentation overlay (Help Center runtime-mounts a gated
  overlay; USB build + `rzfz package` deliver it into `overlay/enterprise/`;
  #125/#126/#129).
- Public delivery hardened: Community Wiki publish (#123), export cull to
  customer-need-only (#124), default `RAZZFAZZ_CHANNEL=public` for Codeberg boxes
  (#127), single-source `RAZZFAZZ_PUBLIC_REMOTE` (#130).
- Fixes: pre-reorg (2026.04/05/06-ga.x) → 2026.07 made the single supported
  bootstrap crossing (operator decision B; #113/#128); `rzfz upgrade --check` no
  longer aborts on absent Paperclip/Matrix keys (#131); Moltis image-builder no-op
  (version-check against `moltis --version`, not `'true'`; #133); init-test
  fixture/timing updates for reorg / #84 / naming / P1 (#131/#132).
- No new `.env` keys over rc2; a box crossing from 2026.06-ga.7 still receives the
  cycle's 52 auto-applied env deltas via the `2026.07-rc1` entry.
- Ships with the full Mode-A security assessment (STRONG, cleared for release, no
  blocker) and a per-image SBOM + CVE diff vs v2026.06-ga.7.

## v2026.07-rc2 (2026-07-05) — second candidate
- rc1 + Onyx v4 clean-install `onyx_user` CREATEROLE (#111), the 2026.04-ga.x
  big-bang bootstrap `rzfz`+`cli/` seed (#113), and the finalised licensing naming
  — rzfz.ai Subscription (tier) / the rzfz.ai Stack (product) (#110).
- No new `.env` keys over rc1.

## v2026.07-rc1 (2026-07-04) — first candidate (consolidated)
- Open-core licensing reorganisation: Apache-2.0 Community tier, BSL-1.1
  source-available tier (rzfz.ai Subscription, rolling Change Date 2029-07-08),
  bundled upstreams under their own licences; root LICENSE/NOTICE/THIRD-PARTY files
  + dynamic `license.<domain>` (#26/#101/#105).
- Coding-agent split into per-type sandboxed containers with Hermes v0.18
  dashboard, live port preview, three-layer memory governance (#84); per-user MCP
  manager with encrypted credential vault + two-tier Cognee memory unified into one
  "MCP & Agent Manager" (#36/#88/#61); central password broker (#54).
- Dify de-vendor + 1.14.2 → 1.15.0 (#107); true SSO group→role mapping (#33) and
  drop of the double login (#18).
- `rzfz` unified CLI + `modules/core/cli/config` repository reorganisation
  (#26/#34); `setup.<domain>` web wizard retired (#22); razzfazz.ai → rzfz.ai
  rebrand (#35); public-delivery Codeberg mechanism (#27/#28).
- Box-wide LLM 429 backpressure limiter (#19, off by default); Dify internal
  document-tools outbound-access allow-list + docling default extractor (#23).
- Version-currency + CVE sweep (Open WebUI 0.10.2, Gitea 1.26.4, SearXNG 2026.7.3,
  Crawl4AI 0.9.0, ClickHouse 25.8, LightRAG v1.5.4, Cognee 1.2.2, gpustack:vulkan
  base rebuild #20); edge-tts removed — Speaches now covers STT + TTS (#68).
- A box crossing from 2026.06-ga.7 receives 52 auto-applied env deltas (2 breaking,
  both auto-migrated) and rebuilds/pulls images.
