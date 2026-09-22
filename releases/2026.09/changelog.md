# Changelog — 2026.09

Engineering view. The customer-facing narrative is in
[`RELEASE_NOTES.md`](RELEASE_NOTES.md).

**Scope of the cycle (2026-09-13, before the GA cut):** 3,121 commits since `v2026.08-ga.15`, 529 issues closed in the
milestone, 243 environment migrations recorded under `2026.09-rc1`, five new profiles
(`llm-manager`, `llm-registry`, `llm-worker-agent`, `openuem`, `wazuh`), fourteen new
manifest entries and **nineteen version bumps** of existing images (Open WebUI 0.10.2 →
0.11.3, Dify 1.16.0 → 1.17.1, authentik 2026.5.5 → 2026.5.7, Komodo 2.2.0 → 2.3.3,
Docling-serve 1.27 → 1.32, LightRAG, Gitea, Gotenberg, Valkey, Crawl4AI, Element,
Stirling-PDF, Vaultwarden, cognee-mcp, the Dify plugin daemon, Hermes, Moltis). The
per-module upstream deltas are in `module-upstream-changes.md`.

## LLM Manager and the GPUStack cutover

The largest single body of work in the cycle: 323 commits across the manager,
its console, the router and the node agent. The cutover retired GPUStack 2.x and
the separate CPU profile, folded the CUDA line into one GPUStack 0.7.1 service
selected by `HARDWARE` plus a device overlay, removed vLLM as a runtime, and made
`https://llm.<domain>/v1` the canonical endpoint every in-stack consumer is wired
to. `/v1-openai` stays as an alias. The manager gained footprint-aware placement,
quantisation selection, expiry for stalled transfers and commands, and a
blue-green runner switch that reports in advance whether it will interrupt
serving — and whether that verdict could be computed at all.

## Wazuh

61 commits building the module from nothing: three services plus bootstrap
one-shots, its own PKI with the certificate tool verified at build time, an
Authentik-gated dashboard, backup and restore wiring, licence and doc-mirror
entries, sixteen environment keys with add-rules, and a host agent whose
file-integrity monitoring is realtime. Enrolment is password-gated;
auto-enrolment ships off pending the release security assessment.

## OpenUEM

23 commits, structured as a numbered task list: database and role, secret
generation on install and upgrade, Caddy site block and forward-auth gate,
Authentik blueprint with group and bindings, PKI and NATS into backup and
restore, five image pins, nineteen environment keys, egress allow-listing, a
start-portal tile, a licence entry and a post-install readiness report.

## Day-1 acceptance tier

62 commits. Full-module rotation with UI-level acceptance: every module with an
interface is driven through Chromium behind Authentik SSO and made to perform one
asserted interaction; modules without an interface need a functional probe or a
recorded reason; a module whose probes all skipped is no longer a pass; probe
specifications carry `MEASURED` or `INFERRED` provenance.

## Agent portal and proxy hardening

57 commits. A portal shell with an agent tree and a live pane, a same-origin
`/i/<token>/` proxy with an owner gate, one-click reconnect and repair,
per-instance naming and process caps, a default-deny egress proxy with an
internal fence overlay, and a series of proxy-correctness fixes: repeated
response headers, redirect rewriting into the pane, write methods on the
same-origin route, and the pane's own framing policy.

## Gate and test-harness work

The cycle surfaced a class of checks that existed but ran in no gate: around
forty static shell suites, the no-build overlay staleness check, and the SSO/OIDC
suite that could report success having rendered nothing. Those were lifted into
the tier that runs on a pull request, the scripts tier was added to the gate, and
a ratchet now refuses a new compose environment key without documentation.

## Observability

Dify and the personal agents state their OTEL transport and sampling rate rather
than inheriting them; an upgrade rewrites stale OTEL values from earlier cycles;
PostgreSQL and Valkey exporters joined the profile; the module's own outbound
product analytics is disabled in the shipped configuration.

## Core, portal and operations

308 commits across Caddy, Authentik, the installer, the upgrade path, the
settings portal, help, licences, backup and status. Highlights: the settings
portal moved to `settings.<domain>` with a one-cycle redirect, licence texts were
vendored in-tree with provenance binding, `rzfz status` gained file-integrity
reporting and canonical-alias awareness, package signature verification reports
"cannot verify" as a distinct state, and per-service database roles are created
before the modules that need them.

## Between rc1 and GA (2026-09-13 onward)

The pass over the open backlog after rc1 was prepared: the authentik healthcheck that
aborted a clean install (#2033), the relay certificate (#2004) and the OpenUEM mail
inheritance (#1992, #2027), Wazuh alert mail (#2002) and the uutils rootcheck signatures
(#1983), the strict online upgrade (#2035), the install's adoption report and the
appliance off-switch (#2006), the ollama-proxy start-up gap (#2036), the
bump-completeness guard and the release check that knows why a rule is absent (#2037,
#198), pins and the manifest's phantom tag (#1969), the second migration validator
(#1993), Dify model plugins pinned (#1972), GPUStack enable repairs (#1973), own-login
declarations for the day-1 probe (#1962), admin agent operations (#1957), agent
instances (#1988), observability consumer parity (#2015), and the LLM Manager failing
under its own name in post-install (#2051).

After the fourteen-way merge: `rzfz init` accepted a retired LLM profile token,
announced the rewrite and installed the retired token anyway — and the install
suite's container map did not know the shipped LLM profiles, so it would not have
noticed on a default box either (#2077, found by the rc1 Phase 3 sweep); a zero-test
run exiting 0 (#2063); the gate's PARTIAL verdict naming its unverified tiers (#2070)
after a silent api tier let one red through (#2067); the docs and local help brought
to the 2026.09 stack (#2057), the security architecture regenerated from a repaired
generator, and the first reference file for consultants' AI agents (#2069). Deferred
to 2026.10 by operator decision: the Dify `.msg` attachment extras (#1981, +8.6 GB
image, one document regressing); recorded as a known issue together with the
air-gapped `.msg` failure (#2073).

**rc2** (2026-09-14) — the rc1 Phase 3 sweep's findings: `rzfz init` no longer
exits on a cognee that is merely slow to report healthy (#2091); the agent
API answers 404 rather than 403 for a user without an instance (#2093); the
not-provisioned fallback link renders the box's own domain (#2109); the
retired-LLM-profile rewrite `init` announces is the one it writes (#2077); a
failing install scenario captures every container's state and the install
suite asserts the exit status of the one-shots it runs (#2097, #2081, #2082,
#2092, #2083, #2118, #2119); the release-docs lint fails closed on a run that
inspected nothing (#2101); the help refresh names every locally documented app
(#2067).

**rc3** (2026-09-15) — the pre-extraction gate captures its helper's status
and a package whose images are all present is neither extracted nor pulled
(#2120, #271); the Wazuh certificate one-shot drops stale files (#2128);
"present" means "built from this tree" for the appliance's adopted images
(#2006, #2105); the migration baseline is the newest release tag of either
form (#2123).

**rc4** (2026-09-15) — journey findings on rc3: per-hardware model defaults
so a CPU box provisions a CPU chat model (#2158); one refused deploy no longer
abandons the standard set (#2156); `init` reads `HARDWARE` from the `.env` it
wrote (#2155); the offline package fails closed on a missing llama runner
(#2154); federation registers every GPUStack model with its state (#1442);
package build first, no registry reach for id-match provenance (#2167, #2168);
the acceptance tier asserts the box answers a prompt (#2163); `rzfz status`
names stuck deployments and the manager trio (#1713, #2164); a module enabled
from the portal gets its manager service key (#2149); the post-upgrade
default-models gate resolves the defaults through the hardware catalogue
(#2193); a partial served list is a readiness state (#2195, #1507). The
Wazuh dashboard's local admin login (`WAZUH_AUTH_MODE`) travels as this
cycle's second migration block.

**rc5** (2026-09-15) — journey findings on rc4: host hardening defers instead
of aborting an upgrade (#2216); the verify suite's Authentik probe runs inside
the container and tells an exec failure from a service failure (#2213).
GA-head items merged after the rc4 tag: the CLI link falls back to
`~/.local/bin` without passwordless sudo (#1941); the offline gate's printed
build command derives its context (#2208); the posture report gains a
passwordless-sudo section (#2210). Rule recorded: `rzfz upgrade` can never
gain an option usable on the hop that delivers it.

**rc6** (2026-09-16) — journey B findings on rc4, measured under a real air
gap: the package carries the Dify plugin packages and the plugin daemon's
dependency cache, and an offline install seeds the cache, uploads each plugin
and reads the install's own verdict (#2222); the Help Center joins the offline
belt (#2223); the sudoers section says UNVERIFIED when it cannot read (#2210).
rc6 was tagged and never installed: its package build stopped on a default
box (#2230).

**rc7** (2026-09-16) — the packager resolves the model volumes without the
legacy inference container (#2230) and names a helper image the box has
(#2225). Never installed: its package bundled a wrong file under a wildcard
catalogue name (#2233).

**rc8** (2026-09-16) — the packager refuses a wildcard weight name where it
cannot be attributed (#2233); the first-boot probe test measures the event it
waits for (#2228). Never installed: its package build exited 127 on an
apostrophe inside the packager's quoted copy script (#2240).

**rc9** (2026-09-16) — the packager's helpers hand their output to the
invoking user and the archive is read back by a second reader before it is
published (#2241); the copy script carries no apostrophe and a guard checks
that bash and the tests build the same script (#2240). First candidate whose
packager ran to completion on a box before the tag; the four journeys run on
it. Known and disclosed: the completeness check is blind to the hardware rule
(#2234), a dead catalogue alias (#2236), two earlier cut records with an
inherited control (#2238), engines surviving a stack stop (#2226), and the
operator decisions on the Workflow Automation tile (#2246) and API token
lifetime (#2247).

**rc10** (2026-09-16) — the package's model weights go into the volume of
the runtime the box actually runs, flat by file name for the LLM Manager
(#2227); the node agent is told the box's network mode (#2219); the installer
decides the release channel again after it replaces stale secrets, so a
re-initialised box no longer lands on the public channel by accident (#2255);
the release-key ordering in the upgrade test compares numerically (#2258).
Cut only after its packager had run to completion on a box.

**rc11** (2026-09-17) — the LLM Manager and its router are told the box's
network mode (#2264); the plugin daemon's environment builder is told the box
is offline through a bound uv configuration, because its resolver never saw
the container environment (#2261); the offline upgrade's package arm is
documented as running the source release's script (#2260, fix in 2026.10).
Journeys: A green, C green with the CPU legacy box unable to embed (#2262,
known issue), D green, B red — zero of three plugins installed air-gapped.

**rc12** (2026-09-18) — the plugin daemon resolves plugin dependencies from a
wheelhouse the package carries, built and verified inside the daemon image at
packaging time; its pip-mirror auto-detection is off and the knob is
documented (#2272); the ClickHouse merge ceiling follows its memory budget
(#2270); assessment documents are swept for secret values on the commit path
(#2275). Cut from main 153b293f4 after the packager ran on 0.91 (3 of 3
plugins, 74 wheels) and a B-shaped check on 0.175 installed 3 of 3 with zero
outbound packets under a positive-controlled counter. Record, so the journeys
are read for what they prove: journey B must establish its own egress cut,
because 0.175's earlier cut is runtime-only and does not survive a reboot;
the packager's unseen-plugins failure and NO-REQUIREMENTS marker paths are
covered by fixture tests only, no box has exercised them; the packager's
model-weight row in the pre-cut run is vacuous because that run did not
request models, not because the building box lacks them (it carries four of
the ten declared); a package with models is built with the completeness rule
in force (#759), and a box whose manager volume is flat cannot attribute the
two wildcard-named models (#2233), so no such box produces a complete set. Red on
main outside the candidate: the legacy runner's network-mode suite (#2277).

**rc13** (2026-09-19) — the candidate GA is cut from, carrying the eight
fixes the operator's stocktake at rc12 pulled forward: the Workflow
Automation tile follows its group entitlement (#2246); API tokens may live
thirty days (#2247); the unservable catalogue alias is gone (#2236);
`rzfz stop` and `rzfz down` stop the LLM engines (#2226); FerretDB's
telemetry is off and the decision is remembered by FerretDB in both
directions (#2267); the security discovery classifies the supervised
engines as their own class (#2268); the upgrader no longer mistakes a tag
for a branch whose tail matches (#2287); `rzfz start` redeploys the models
after a `down` (#2289). Cut from main ff8193b86 after a pre-cut packager run
with models under the disclosed completeness override. Record: the #2287
classification runs in the SOURCE release's script before the re-exec, so no
rc13 journey demonstrates that fix end to end — a box upgrading from rc12
runs rc12's broken classifier, and a box already on rc13 has nothing to
upgrade to; what was measured is the two halves separately, the unfixed
classifier calling the real tag a branch from an rc12 source and the fixed
classifier on a box at rc13 deciding correctly against the same live
collision; the full hop with the fixed tree as the source is first
exercised at 2026.09-ga → ga.1. 2026.08-ga.15 has no classifier at all, so
the ga.15 crossing (journey C, shape 1) was never affected. Journey
records on rc13, every run with the tree asserted by content and the tag
fetched by the script: A green on the head test box (a Strix Halo in CPU
mode; eighteen minutes to four models served and an embedding vector); D
green, the package upgrade from rc12 under a proven cut, the package's five
weight files including the vision projector placed in the Manager's volume
with no download; B green, the fresh offline install from the package with
the full image set loaded and no pull, three plugins from the wheelhouse,
Dify's model provider registered, and the posture report naming the three
declared models the package could not carry — the disclosure working; C
shape 1 green, a genuine 2026.08-ga.15 single-box on the legacy runtime
with the customer's document-extraction model running, upgraded online
from a cold tag: the identical model set served through the Manager
afterwards, the model neither copied nor downloaded again, vision and
embedding requests answered, data preserved — the upgrade federates the
legacy runtime behind the Manager and moves no weights. The offline
crossing from the same baseline, driven as the package path drives it by the
2026.08 upgrader, showed that upgrader neither adds the GPU device overlay
2026.09 requires nor the LLM Manager, and that its rollback restores
configuration only; run with the package's own upgrader placed over the
installed tree first (the operator's chosen path), the crossing added both,
placed the package's weights without touching the customer's, served the
same four models, and moved the host interface by a hundred megabytes under
the cut. A seeded run then showed existing Dify workflows and knowledge bases
bound to the legacy provider keep working across the crossing while that
runtime is retained; only the defaults move to the canonical endpoint.

**2026.09-ga (tagged 2026-09-21, commit 38f0740bf).** The release commit on the
rc13 code as measured.
The fresh-install findings of the cut were re-measured on a healthy box and withdrawn
as release defects; the upgrade path was clean throughout.

**2026.09-ga.1 (tagged 2026-09-22).** Patch on 2026.09-ga, no image pin moves: #2367 weight download resume and retry, whole-weight launch, post-install early exit on a dead failed set; #2320 Dify plugin pin on an upgrade from a package (Enterprise); #2324, #2327, #2332 fresh-install seeder; #2368 verify-images network-mode aware; #2352 package accepts a worktree; #2353 package archive ignored by the upgrader; #2359 the export stamps its source commit (RAZZFAZZ_SOURCE_COMMIT); #2366 public release-page images via the tree; #2365 release tooling out of the export; #2362 module descriptions in customer language. Measured on the fleet: clean mirror installs on 0.79 (605 s), the ga→ga.1 hop on 0.91.
