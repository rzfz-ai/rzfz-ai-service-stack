<!-- section: get-started -->
<!-- audience: end-user -->
# Community install

This page walks you through installing the Community stack — the rzfz.ai Stack,
the integrated open-source stack for local AI infrastructure — on your own host,
from cloning the code to opening the UIs behind single sign-on.
It is public-safe and self-contained: everything here is free to run for private
and evaluation use.

If you haven't yet, skim [Getting started](getting-started.md) first for the
prerequisites and the shape of the install.

## Before you begin

Make sure your host meets the prerequisites — a supported Linux host, Docker with
Compose v2, and enough RAM and disk. The details are in
[Getting started → Host requirements](getting-started.md#host-requirements). In
short:

- **Ubuntu 26.04 LTS** (the supported base).
- **Docker Engine 24.0+** with the **Docker Compose v2** plugin.
- **32 GB RAM or more**, and disk to match your model choices.
- A base domain decided (for example `myai.local` for a local install, or a real
  domain if you want automatic TLS).

Verify Docker is ready:

```bash
docker --version
docker compose version
```

Both must succeed. `docker compose` (with a space) is the v2 plugin; the legacy
`docker-compose` v1 binary is not supported.

## Step 1 — Get the code

Clone the public Community repository from GitHub:

```bash
git clone https://github.com/rzfz-ai/rzfz-ai-service-stack.git
cd rzfz-ai-service-stack
```

This is the source-of-truth public mirror for the Community stack. Anonymous
clones work — no account or token needed.

## Step 2 — Run `rzfz init`

`rzfz init` is the first-time installer. It checks prerequisites, generates all
secrets, applies your configuration, builds and pulls the container images, and
starts the stack. There is no separate web "setup wizard" — first-time setup is
`rzfz init`.

### Option A — Interactive (recommended for a first install)

```bash
rzfz init
```

The installer asks for your domain, timezone, admin password, and which modules
(profiles) to enable, then does the rest. This is the easiest way to get a feel for
the options.

### Option B — A preset (non-interactive)

A **preset** bakes in a whole scenario; you supply just the domain and password:

```bash
# AMD single box, self-signed TLS, a common set of modules (hardware auto-detected)
rzfz init --package single-box --domain myai.local --password 'SecurePass123!'
```

Other presets cover a CPU-only host and a control-plane node; see
`rzfz init --help` for the full list of presets and flags. You can also spell out
everything explicitly:

```bash
rzfz init --domain myai.local --timezone Europe/Berlin \
    --password 'SecurePass123!' --profiles chat,dify,monitor \
    --hardware cpu --tls-mode selfsigned --skip-interactive
```

Run `rzfz init --help` at any time to see the authoritative option list.

## Step 3 — Let it come up

On first start the installer builds custom images, pulls the rest, starts the
containers, and waits for identity (Authentik) to initialise — that first
initialisation typically takes a couple of minutes. When it finishes, `rzfz init`
prints the URLs for your enabled apps.

## Step 4 — Verify health

Check that the containers are up:

```bash
docker compose ps
```

Every container should show `running` (or `healthy`). If one is stuck, look at its
logs:

```bash
docker compose logs -f <container-name>
```

## Step 5 — Deploy the default models

A fresh install has the apps but no models yet. Provision the default model set so
chat and retrieval work:

```bash
rzfz post-install --preset standard
```

This deploys a sensible default (a general chat model, embeddings, and a reranker)
and wires the apps to GPUStack. Model downloads can take a while; you can verify
afterwards with:

```bash
rzfz post-install --verify
```

## Step 6 — Access the UIs behind SSO

Open the apps in your browser at subdomains of your base domain. Each one is gated
by single sign-on: the first time you visit, you log in through **Authentik**, and
it hands you to the app. From then on you move between apps without logging in
again.

- `https://chat.<domain>/` — Open WebUI (chat)
- `https://dify.<domain>/` — Dify (workflows)
- `https://llm.<domain>/` — GPUStack (LLM management)
- `https://auth.<domain>/` — Authentik (identity)
- `https://settings.<domain>/` — Configuration portal
- `https://help.<domain>/` — in-product Help

> For a **local** install (self-signed TLS, no public DNS), add `/etc/hosts`
> entries pointing each subdomain at the host so your browser can resolve them.
> Your browser will warn about the self-signed certificate — that's expected for a
> local, non-public install.

## What next

- **[Getting started](getting-started.md)** — the on-ramp, if you skipped it, and
  the "what you get" tour.
- **[Architecture overview](architecture-overview.md)** — understand the front
  door, the core, and how a request flows.
- **Community support → [GitHub Issues](https://github.com/rzfz-ai/rzfz-ai-service-stack/issues)**
  — questions and bug reports, public and free.
- **Enterprise documentation → [docs.rzfz.ai](https://docs.rzfz.ai)** — the gated
  operations manual (operations, upgrades, hardening, SSO, compliance), part of the
  rzfz.ai Subscription.
