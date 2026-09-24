<!-- section: get-started -->
<!-- audience: end-user -->
# Getting started

The rzfz.ai Stack — the integrated open-source stack for local AI infrastructure —
is designed to go from a bare Linux host to a working AI platform in a single
command. This page is the friendly on-ramp: what you need, the quickstart, and
what you get. When you are ready for the step-by-step, head to
[Community install](community-install.md).

## Prerequisites at a glance

You need three things:

- **A Linux host.** Ubuntu 26.04 LTS is the supported base. Everything runs in
  containers, so you do not install service packages on the host itself.
- **Docker + Docker Compose v2.** Docker Engine 24.0+ with the Compose v2 plugin
  (`docker compose`, with a space — not the legacy `docker-compose` v1 binary).
- **Enough hardware.** Plan on **32 GB RAM or more** for a useful deployment — the
  always-on core alone (database, identity, cache, proxy, mail relay, and the
  built-in UIs) needs well over 16 GB before you load a single model.

For LLM inference the stack supports three hardware targets, chosen at install
time and auto-detected if you don't say:

- **AMD** (e.g. Strix Halo, with integrated/unified memory)
- **NVIDIA** (CUDA GPUs)
- **CPU-only** (no GPU — works, but slower; fine for light or batch use)

See [Host requirements](#host-requirements) below for a little more detail.

## The three-line quickstart

On a prepared host with the code checked out:

```bash
rzfz init                 # 1. run the interactive setup wizard
                          # 2. answer domain / timezone / password / profiles
                          # 3. open the UIs it prints when it finishes
```

`rzfz` is the unified command-line tool for the stack — one entry point,
`rzfz <command>`. `rzfz init` checks prerequisites, generates all secrets, asks
for your domain, timezone, admin password, and which modules to enable, then
builds and pulls the container images and starts everything.

Prefer a non-interactive install? Pass a **preset** that bakes in a whole
scenario:

```bash
# AMD single box, self-signed TLS, common modules (hardware auto-detected)
rzfz init --package single-box --domain myai.local --password 'SecurePass123!'
```

The full walkthrough — cloning the code, presets, verifying health, opening the
UIs — is in [Community install](community-install.md).

## What you get

When `rzfz init` finishes and you run post-install provisioning, you have a
running AI platform reachable on subdomains of your base domain, all behind
single sign-on:

- **Chat** (`chat.<domain>`) — Open WebUI, talking to local models.
- **Workflows** (`dify.<domain>`) — Dify for building LLM apps.
- **LLM management** (`llm.<domain>`) — GPUStack, where your models load and run.
- **Identity** (`auth.<domain>`) — Authentik, your single sign-on and user
  management.
- **Configuration portal** (`settings.<domain>`) — enable modules and change
  settings after install.
- **Help** (`help.<domain>`) — in-product documentation, served offline.

Plus the optional modules you enabled — monitoring, private web search, speech,
document conversion, agents, and more.

## First-run pointers

- **Open the UIs behind SSO.** Every app sits behind Authentik. The first time you
  visit one, you log in through Authentik and it hands you to the app. See
  [Architecture overview](architecture-overview.md) for how that gate works.
- **Deploy the default models.** A fresh install has the apps but no models yet.
  Post-install provisioning deploys a sensible default model set (a general chat
  model, embeddings, and a reranker) so chat and retrieval work out of the box.
- **Enable more modules any time.** Modules are Compose profiles; turn them on
  from the Configuration portal or by editing your profile list.

## Host requirements

A little more detail on the "enough hardware" note above:

| You are running… | Typical RAM | GPU / VRAM | Free disk |
|---|---|---|---|
| A single AMD box (integrated/unified memory) | 128 GB unified is the reference appliance; core reserves a ~32 GB container budget, the rest is model memory | integrated (unified) | 250 GB+ NVMe |
| A CPU-only host or VM (no GPU) | 32 GB+ for the core, **plus** model RAM on top (CPU inference runs in RAM) | none | 100 GB+ |
| An NVIDIA / CUDA host | 32 GB+ host RAM for the containers | VRAM by your GPU budget | 100 GB+ |

Notes:

- **Ubuntu 26.04 LTS is the supported operating system.** The stack is tightly
  bound to its Docker / Docker Compose architecture and assumes this base.
- **All apps are reached through Caddy on ports 80/443.** Database and internal
  service ports bind to `127.0.0.1` only. For public DNS + automatic TLS you'll
  point A records (or a wildcard `*.<domain>`) at the host; for a purely local
  install, `/etc/hosts` entries are enough (self-signed TLS).

The complete per-hardware sizing matrix and the full support statement live in the
**Enterprise documentation** at [docs.rzfz.ai](https://docs.rzfz.ai).

## Next

- **[Community install](community-install.md)** — the full step-by-step.
- **[Architecture overview](architecture-overview.md)** — understand the pieces
  before or after you install.
