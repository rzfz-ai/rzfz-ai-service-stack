# rzfz.ai Service Stack

<p align="center">
  <img src="core/Authentik/media/razzfazz.png" alt="rzfz.ai Logo" width="300">
</p>

A complete, **self-hosted, modular AI service stack** — chat UI, LLM inference, workflow
automation, identity management, monitoring and more, all running as Docker Compose
containers and toggled at the module level via Compose profiles. Includes Open WebUI
(chat), GPUStack (LLM management), Dify (workflow automation), Authentik (SSO), and
~25 other optional modules. Caddy is the single external entry point; every service sits
behind Authentik SSO.

---

## 🚀 Quick start

On a fresh **Ubuntu 26.04 LTS** host (the only supported OS) with Docker Engine 24.0+ and Docker Compose v2:

```bash
# 1. Install Docker (if not already present)
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER
#    Log out and back in for the group change to take effect.

# 2. Clone the repository
git clone https://github.com/rzfz-ai/rzfz-ai-service-stack.git
cd razzfazz-ai-service-stack

# 3. Run first-time setup
rzfz init
```

> `rzfz` is the unified CLI for the stack (one entry point — `rzfz <command>`).
> The legacy `razzfazz-*.sh` scripts still work as thin shims, but `rzfz` is the
> canonical form. Run `rzfz help` for the command list.

`rzfz init` checks prerequisites, generates all secrets, asks for domain / timezone /
admin password / profiles, builds and pulls images, and starts the stack. There is **no
separate web "setup wizard"** — first-time setup is `rzfz init`; later changes go through
the **Configuration Portal** (`https://config.<domain>/`).

**Non-interactive, with a preset:**

```bash
# AMD GPU single box, self-signed TLS, all modules (HARDWARE auto-detected)
rzfz init --package single-box --domain myai.local --password 'SecurePass123!'

# Or spell out the options explicitly:
rzfz init --domain mycompany.ai --timezone Europe/Berlin \
    --password 'SecurePass123!' --profiles chat,dify,llm-cpu,monitor \
    --hardware cpu --skip-interactive
```

Presets: `single-box` (AMD GPU, llm-legacy), `master-cpu` (Let's Encrypt, Google SSO,
llm-cpu, master mode), `testvm-cpu` (self-signed, llm-cpu + Gitea), `worker-box`
(GPUStack worker node only). Run `rzfz init --help` for every flag.

📖 **Full installation guide** (requirements, presets, offline/air-gap, first boot,
post-install provisioning): **the in-product Help-UI**.

---

## 📚 Documentation

This README is just a landing page. The real documentation lives in three places:

| Where | Audience | What |
|-------|----------|------|
| **In-product Help-UI** — `https://help.<your-domain>/` | Customer staff & on-site admin | The customer docs below, rendered inside the running stack behind SSO. |
| **Internal Gitea wiki** — `…/razzfazz-ai-service-stack.wiki.git` | razzfazz.ai operator / dev / tester | Architecture, deployment-options matrix, sizing, security controls, release process, operator runbook, testing & known-issues. |

### Customer documentation

The full customer documentation — install & first boot, tutorials, how-to guides,
identity / SSO, reference and troubleshooting — ships **inside the running stack**,
rendered behind SSO at `https://help.<your-domain>/`.

### Web UIs (all behind Authentik SSO)

`chat` · `dify` · `llm` (GPUStack) · `auth` (Authentik) · `admin` (Komodo) ·
`config` (Configuration portal) · `backup` · `help` —
each at `https://<name>.<your-domain>/`.

---

## 📦 Modules & profiles

Modules are enabled by editing `COMPOSE_PROFILES` in `.env` (or via the **Configuration
Portal**). The always-on **core** (Caddy, PostgreSQL, Authentik, Valkey, SMTP relay,
Backup, Config Portal, Help) needs no profile. A few of the ~25 optional profiles:

| Profile | Services | Purpose |
|---------|----------|---------|
| `chat` | Open WebUI, Pipelines | AI chat interface |
| `dify` | Dify API/Worker/Web/Sandbox | Workflow automation |
| `llm` | GPUStack v2.1.x + custom backends | LLM inference (`HARDWARE=amd\|nvidia\|cpu`) |
| `llm-legacy` / `llm-cpu` | GPUStack v0.7.1 | Stable LLM paths (AMD Strix Halo / CPU) |
| `monitor` | Komodo | Container monitoring UI |
| `searxng` | SearXNG | Private metasearch (backend for Chat + Dify) |
| `gitea` | Gitea | Self-hosted Git |

> `llm`, `llm-legacy`, and `llm-cpu` are **mutually exclusive** — only one LLM profile is
> active at a time. `rzfz init` auto-detects `HARDWARE` and writes the matching
> `COMPOSE_FILE` overlay (`modules/llm/compose.devices.<amd|nvidia|cpu>.yml`).

📖 **Full profile table** (all modules, services, experimental flags) and the
hardware × LLM-profile × preset matrix:
**the in-product Help-UI** and
**deployment-options.md**.

---

## 🛠️ Management — the `rzfz` CLI

One unified entry point — `rzfz <command>` (run `rzfz help` for the list). The legacy
`razzfazz-*.sh` scripts remain as thin shims, but `rzfz` is canonical.

| Command | Purpose |
|---------|---------|
| `rzfz init` | First-time setup: prereqs, secrets, build, start, Authentik init |
| `rzfz post-install` | Post-install provisioning: deploy models, configure services |
| `rzfz setup` | Config: secrets rotation, API keys, SMTP, TLS, checksum governance, reset |
| `rzfz backup` | Backup / restore of Docker volumes (`list` · `backup` · `restore` · `status`) |
| `rzfz upgrade` | Stack upgrades: git pull or offline package, `.env` migration, health verify |
| `rzfz status` | Health / configuration report of the running box |
| `rzfz package` | Create offline upgrade packages from a git tag (developer side) |
| `rzfz checksum` | SHA256 governance snapshots for change detection / audit |
| `rzfz logs` | Collect container logs + system info into a support archive |

Every command supports `--help`. 📖 **Full command index with one-liners and danger
classification:** **the in-product Help-UI**.

After changing `.env`, apply with `docker compose up -d --force-recreate` — `docker compose
restart` does **not** reload `.env` changes.

---

## 🔒 Security

The stack is built to be locked down by default, but a production deployment must still be
hardened:

- **Rotate all secrets** — `rzfz init` generates them automatically on first install;
  on an inherited box run `rzfz setup --secrets-status` / `--regenerate-secrets`.
  Never commit `.env` / `.env.dify` to version control.
- **TLS** — use Let's Encrypt (`--tls-mode letsencrypt`) or a custom wildcard
  (`--tls-mode certificate`) in production; self-signed is dev/local only. Caddy manages and
  renews certificates.
- **Network** — all database/service ports bind to `127.0.0.1`; Caddy (ports 80/443) is the
  sole external entry point and every UI is gated by Authentik SSO. Enable a host firewall
  (ufw) allowing only 80/443. GPUStack `master`/`worker` modes open extra ports — restrict
  them to trusted worker IPs.
- **Containers** — the Docker socket is mounted for a few services (authentik-worker,
  gpustack); review `seccomp`/capability settings before running untrusted workloads.

📖 Identity, MFA and SSO setup live in the in-product Help-UI;
secret-rotation, cert-renewal and factory-reset procedures in
the in-product Help-UI; the full as-built security architecture is in
the in-product Help-UI and the internal Gitea wiki.

---

## 📄 License & support

- **License:** Open-core. The Community components are **Apache-2.0** (free, including private/eval,
  no key or registration); our first-party enterprise/orchestration components are
  **source-available under BSL 1.1**, governed by the **rzfz.ai Subscription** for commercial
  production and converting to Apache-2.0 on a rolling Change Date; bundled upstreams keep
  their own licences. Entity **razzfazz.ai GmbH – Member of SEQIS Group**, Vienna. See
  [LICENSE](LICENSE), [docs/LICENSING.md](docs/LICENSING.md), [NOTICE](NOTICE) and
  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md); the live component/licence map is at
  `https://license.<your-domain>/`.
- **Support:** razzfazz.ai support contact — `support@razzfazz.ai`. From a running box you can
  collect a diagnostic log snapshot with `rzfz logs take "<reason>"` (see
  report-a-problem.md) and attach it.
