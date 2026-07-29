<!-- audience: end-user -->
# The rzfz.ai Stack — Community Wiki

The rzfz.ai Stack — the integrated open-source stack for local AI infrastructure.
It brings a chat UI, private LLM inference, workflow automation, single sign-on,
and monitoring together into one self-hosted platform that runs entirely on
hardware you control.

This is the **Community Wiki (on GitHub)** — the public documentation for the
Community stack. It covers getting started, the architecture, and installing the
stack yourself. It is free to read, and the projects it documents are free to run
for private and evaluation use.

> ## 📢 What's new & release notes
>
> The stack ships on a monthly **General Availability** cycle, with patch releases in
> between. Start here to see what changed:
>
> - 🆕 **[What's New](https://github.com/rzfz-ai/rzfz-ai-service-stack/blob/main/releases/{{LATEST_CYCLE}}/WHATS_NEW.md)**
>   — the highlights of the current release, in plain language.
> - 📋 **[Full release notes](https://github.com/rzfz-ai/rzfz-ai-service-stack/blob/main/releases/{{LATEST_CYCLE}}/RELEASE_NOTES.md)**
>   — every change, fix, and upgrade note for the current cycle (GA + all patches).
> - 🗂️ **[All releases & version history](https://github.com/rzfz-ai/rzfz-ai-service-stack/releases)**
>   — browse every published version, newest first.
>
> These are the same notes shown in your box's in-product **Configuration Portal**.

## What it is

The rzfz.ai Stack is a **self-hosted, modular, Docker-Compose AI service stack**.
Everything runs as containers on a single Linux host, and you turn capabilities on
and off at the module level via Docker Compose *profiles*. Out of the box you get:

- **Chat UI** — Open WebUI for talking to your models.
- **LLM inference** — GPUStack serving language models locally (AMD, NVIDIA, or CPU).
- **Workflow automation** — Dify for building LLM apps and pipelines.
- **Identity & single sign-on** — Authentik in front of every app.
- **Monitoring, search, document tools, agents, and more** — around **25 optional
  modules**, each a Compose profile you enable when you need it.

Two design choices define the stack:

- **Caddy is the single entry point.** One process listens on ports 80/443,
  terminates TLS, and gates every app.
- **Authentik provides single sign-on.** You log in once, and Authentik decides
  which apps you can reach.

See the [Architecture overview](architecture-overview.md) for how these fit
together.

## Who it's for

The stack is for teams and individuals who want modern AI capabilities — chat,
retrieval, workflows, agents — **without sending their data to a third-party
cloud**. Everything stays on your own appliance or server. It suits privacy-
conscious organisations, on-premises deployments, evaluation labs, and anyone who
prefers to own their AI infrastructure.

## Open-core, in one paragraph

The stack follows an **open-core** model. The **Community** components are
**Apache-2.0** — genuine open source, free for any use, **including private and
evaluation use with no key and no registration**. The first-party enterprise and
orchestration components are *source-available* and their commercial production
use is governed by the **rzfz.ai Subscription** (which, on a rolling Change Date,
converts each released version to Apache-2.0). The many bundled upstream projects
(Open WebUI, Dify, Authentik, GPUStack, and others) each keep their own upstream
licences. In short: read, run privately, and evaluate freely; a Subscription
covers commercial production of the source-available parts.

## Community documentation

- **[Getting started](getting-started.md)** — prerequisites at a glance and the
  three-line quickstart.
- **[Architecture overview](architecture-overview.md)** — the module system, the
  always-on core, networking, and how a request flows.
- **[Community install](community-install.md)** — install the Community stack
  yourself, from cloning the code to opening the UIs.

## Where to go next

- **Enterprise documentation → [docs.rzfz.ai](https://docs.rzfz.ai)** — the gated
  operations manual (operations, upgrade runbooks, hardening, SSO and compliance).
  It is part of the **rzfz.ai Subscription** and is not part of this public wiki.
- **Community support → [GitHub Issues](https://github.com/rzfz-ai/rzfz-ai-service-stack/issues)**
  — report a bug or ask a question, publicly and free.
- **Source code → [GitHub](https://github.com/rzfz-ai/rzfz-ai-service-stack)**
  — the public source repository for the Community stack.
