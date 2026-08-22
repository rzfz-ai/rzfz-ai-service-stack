# Licensing — the razzfazz.ai Service Stack open-core model

This document explains, in plain language, how the razzfazz.ai Service Stack is
licensed and offered. The authoritative, machine-readable mapping of every
component to its licence is [`stack.yaml`](../stack.yaml) (fields `license` /
`edition` / `built_from`); the [`NOTICE`](../NOTICE) file lists third-party
attributions; and the `license.<your-domain>` web UI renders the same data live.

> Licensing contact: **licensing@razzfazz.ai**
> Entity: **razzfazz.ai GmbH – Member of SEQIS Group**, Vienna, Austria.

---

## How the offering is structured (three tiers)

The stack is distributed under an **open-core** model. There are exactly three
tiers, and it is important to keep them apart:

1. **Open Source (free).** The Community-Edition code is Apache-2.0 and the full
   source is public on **Codeberg**. **Private, personal, and evaluation use of
   the whole stack needs nothing at all — no subscription, no registration, no
   licence key.** Community support is the **Codeberg issues tracker and wiki**.

2. **rzfz.ai Subscription** (€799 per server per year). This is a
   **LICENCE, not a service.** It grants the right to run the current, patched
   Stack **commercially in production**. It includes: the right to commercial
   production operation of the current patched Stack; licensed
   security-updates / releases (**at least 4 per year**); and access to the
   gated **Enterprise documentation** (docs.rzfz.ai). **It explicitly does NOT
   include any service** — no ticket support, no installation, no
   update-installation. Those are separate paid offerings (tier 3).

3. **Services (separate, optional).** Installation, update installation,
   maintenance, and ticket/SLA support are **separate paid offerings**. They are
   **never** part of the Subscription — you buy them only if you want
   them.

> **The subscription is a licence, not a service.** Paying for the rzfz.ai
> Subscription buys you the *right to run the patched Stack commercially*
> and the *licensed security releases* — it does not buy you anybody's time.
> Installation and support are bought separately.

The rest of this document explains which code sits under which licence, how the
rolling Change Date works, and how we keep the bundled copyleft components clean.

---

## The kinds of material (what each licence covers)

Three kinds of material live side by side in the repository, each under its own
licence.

### 1. Community — Apache-2.0 (**Open Source**)

Our first-party compose wiring, configuration templates, documentation, base
model wiring, and the build recipes of the build-only containers. Marked
`edition: community` / `SPDX-License-Identifier: Apache-2.0`. This is genuine,
OSI-approved **Open Source** — free for any use, including production, and the
source is public on Codeberg. Community support is the Codeberg issues tracker
and wiki. Full text: [`LICENSE-APACHE`](../LICENSE-APACHE).

### 2. rzfz.ai Subscription components — Business Source License 1.1 (**source-available**)

Our first-party application and orchestration code: the Configuration, Help,
Licenses and Start portals; the shared runtime library
(`core/common/razzfazz_common`); Model Sync; Backup Management; the Agent Manager
and MCP Manager; and the razzfazz.ai orchestration surface (`scripts/`, `cli/`,
provisioning, migrations, security-review tooling, Authentik SSO provisioning
templates). Carrying an `SPDX-License-Identifier: BUSL-1.1` header.

**The BSL 1.1 is _source-available_, NOT open source.** It does not meet the
Open Source Definition (it restricts commercial production use), so we never call
these components "open source." You may:

- read the source, copy it, and modify it;
- make **private, personal, and non-production use** (evaluation, development,
  testing, internal non-production) freely, at **no charge and without any
  subscription or registration** — always, for anyone.

**Commercial production use requires a valid rzfz.ai Subscription.** The
subscription is a **licence, not a service**: it authorizes commercial production
operation of the current, patched Stack and delivers the licensed security
releases (at least four per year) and the gated Enterprise documentation.
Installation, update installation, and support are **separate offerings** and are
never part of the subscription.

A subscription that was valid when a given version was released **permanently
authorizes commercial production use of that version** — expiry or non-renewal
does not revoke your right to keep running the versions covered during your
subscription term. Renewal simply provides newer licensed versions and security
releases. On that version's Change Date the code converts to the Change License
(Apache-2.0) anyway. Full text and the filled BSL parameters:
[`LICENSE-BSL`](../LICENSE-BSL).

> **No license key required.** These containers ship with **no license-key
> check** and run freely out of the box — the subscription governs *permitted
> use*, not *technical function*. Private, personal, development, testing and
> evaluation use is always free, with no subscription and no registration.
> Holding a valid Subscription for your **commercial production** use is a
> licence obligation, not something the software enforces.

> Only the **Community** tier is "Open Source." The BSL 1.1 components covered
> by the **rzfz.ai Subscription** are "source-available." Please keep this
> distinction in all customer-facing text.

### 3. Bundled / built upstream components — their own upstream licences

The stack bundles or builds many independent upstream projects (Open WebUI,
Dify, GPUStack, Authentik, PostgreSQL, Caddy, Komodo, SearXNG, Synapse, Element,
Vaultwarden, paperless-ngx, …). **Each runs as its own container and keeps its
own upstream licence — we do not relicense them.** They are marked
`license: upstream:<SPDX>` in `stack.yaml`. Our contributions to a built image
(Dockerfile, config, patches, wiring) are at most Apache-2.0, never BSL — see the
per-image `NOTICE` files.

---

## The rolling BSL Change Date

Each **published version** of the source-available (BSL 1.1) components converts
to Apache-2.0 **three years after that version's release date** (and never later
than the fourth anniversary of its first public distribution — the BSL cap).
Every version has its own Change Date; the authoritative schedule is
[`config/manifests/license-dates.json`](../config/manifests/license-dates.json),
extended automatically by the release pipeline on each GA tag.

| Version | Released | Change Date (→ Apache-2.0) |
|---|---|---|
| 2026.07-ga (first public release) | 2026-07-08 | 2029-07-08 |

So the source-available code you run today becomes fully open source three years
on, while the newest release stays source-available — the standard BSL bargain.

---

## How we keep copyleft (GPL / AGPL) clean

Several bundled components are strong copyleft:

- **AGPL-3.0**: Vaultwarden, SearXNG, Synapse, Element Web
- **GPL-3.0**: Komodo, paperless-ngx

Two design rules keep this uncomplicated:

1. **Mere aggregation.** Every upstream runs as its **own container / process**,
   communicating over network interfaces — analogous to pipes and sockets. Under
   the FSF's own guidance this is *mere aggregation*: it does **not** extend
   copyleft into our BSL / Apache code. We never link a GPL/AGPL library into our
   own process. (The one weak-copyleft library we *do* import, `psycopg2` /
   LGPL-3.0, is used unmodified and is dynamically linked and user-replaceable —
   see `NOTICE`.)

2. **We ship them UNMODIFIED.** We *wire and configure* these upstreams; we do
   **not** fork or patch them into our build. AGPL §13's network-copyleft
   obligation is only triggered by **modifying** the program and then exposing
   it over a network. Because our images are the unmodified upstream, §13 is not
   triggered — whether the customer self-hosts or we host a single-tenant
   instance.

   > **Governance rule.** If anyone ever patches an AGPL/GPL component into our
   > build (a rebrand, a compiled-in theme, an in-container `sed`-patch), that
   > component's Corresponding Source — **including our modification** — must be
   > offered to all network users of that instance. Don't do this without legal
   > sign-off and a documented source-offer. If you find such a patch in the
   > tree, flag it.

### Redistribution / Corresponding Source

Publishing the **built GPL/AGPL images** publicly is "conveying," which triggers
the Corresponding Source obligation (GPL/AGPL §4–6) **independent of §13**. GPLv3
§6(d) permits a bare link to the upstream tag, but that puts us on the hook for
that link's continued availability, so we provide a durable **source mirror**:

- `scripts/generate-source-mirror.sh` produces a `git bundle` per redistributed
  copyleft component (Vaultwarden, SearXNG, Synapse, Element = AGPL-3.0; Komodo,
  paperless-ngx = GPL-3.0; and the LGPL psycopg2) at the **exact pinned tag** it
  ships. These bundles are published as **release assets** alongside the public
  snapshot (a release step, like `publish-public.sh`).
- [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) references, per component,
  `source-mirror/<comp>-<version>.bundle` (the release asset) plus the upstream
  tag URL as a secondary reference, with the SPDX and copyright. It is generated
  mechanically by `scripts/generate-notice.py` from the SSOT.

---

## Dify (Apache-2.0 + additional conditions)

Dify is Apache-2.0 with two additional conditions: (1) no multi-tenant operation
without written authorization, and (2) the Dify logo / copyright must remain in
the web image and console. **We comply strictly with Dify's Community Edition
licence conditions** — we do not rely on any special agreement:

- We ship **Dify Community Edition, single-tenant** (one workspace / one instance
  per customer — never a shared multi-tenant instance), which does not require a
  commercial licence under condition (1).
- We keep the Dify branding: the `dify-web` image is **built from the pinned Dify
  web source at build time** and we do not remove or modify the logo/copyright,
  satisfying condition (2).
- We **pass Dify's licence through** unchanged: Dify's own licence text (with the
  additional conditions and the "© 2025 LangGenius, Inc." copyright) ships as
  `modules/dify/DIFY-LICENSE` and is referenced from `THIRD-PARTY-NOTICES.md`.

---

## Trademarks

All of these licences grant copyright rights but **no trademark rights**.
Third-party names and logos (Dify, Open WebUI, Authentik, Vaultwarden, SearXNG,
Element, Komodo, …) are used **nominatively** to identify the bundled components;
no sponsorship or endorsement is implied, and we do not present upstream products
as razzfazz.ai products (or vice-versa). "razzfazz.ai" is a trademark of
razzfazz.ai GmbH.

---

## Open legal-review items (pending counsel)

The following are **flagged for the operator's lawyer** and are intentionally not
finalized here (US-centric research must be reconciled with DE/AT/EU law):

- **BSL Additional Use Grant wording** — the grant (private, personal and
  non-production use free with no subscription and no registration; commercial
  production use requiring a valid rzfz.ai Subscription; perpetual
  commercial production use of any version covered during a valid subscription
  term; the subscription being a licence, not a service, with installation,
  updates and support as separate offerings) is **content-approved by rzfz.ai;
  the exact wording is pending final legal sign-off.** Still to be confirmed in
  the final wording: whether our exclusively hosted single-tenant box counts as
  the customer's production use (it should, but must be watertight).
- **AGPL §13 wording** for the modified-and-hosted case, and the exact
  Corresponding-Source offer mechanism (link vs. mirror vs. written offer).
- **Reconciliation with DE/AT/EU law** — the cited sources are predominantly
  US-oriented.
- **Dify CE agreement scope** — archive the written agreement; confirm it covers
  both redistribution of the CE image and our exclusive-hosting deployment.
- **AGB/BVB compatibility** of BSL 1.1 with the razzfazz.ai terms and the
  rzfz.ai Subscription model.

See the full research and sign-off list in the internal legal-research brief.
