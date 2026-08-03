# 🚀 razzfazz.ai — Release 2026-03.GA

**The March 26 General Availability Release**

*March 2026 · 146 commits · 3 months of engineering*

---

After months of intensive development, hardening, and real-world testing, we're thrilled to announce **razzfazz.ai 2026-03.GA** — the March General Availability release of the razzfazz.ai Service Stack. This milestone marks the transition from internal release candidates to a production-ready, fully self-hosted AI platform that can be deployed with a single command.

---

## ✨ Highlights

### 🏗️ One-Command Installation
The entire stack — from identity management to GPU-accelerated LLM inference — deploys in minutes with `./razzfazz-init.sh`. Choose from pre-configured packages (`single-box`, `master-cpu`, `testvm-cpu`, `worker-box`) or go fully custom. Secrets are auto-generated, databases initialized, blueprints applied, and services health-checked — all without manual intervention.

### 🔐 Enterprise-Grade Identity & SSO
Authentik is now deeply integrated as the identity backbone. All services are protected behind SSO with automatic provider and application provisioning via declarative blueprints. Google SSO, MFA enforcement, admin password change policies, and granular group-based access control are baked in from day one.

### 🤖 Flexible LLM Infrastructure
Run AI models on CPU, AMD GPU (Vulkan), or CUDA — with GPUStack managing the inference layer. The new **master/worker architecture** allows scaling across multiple machines. Custom Jinja chat templates, automatic model synchronization to Open WebUI, and a dedicated watchdog for orphaned GPU processes round out the LLM experience.

### 📦 Automated Upgrades & Backups
The new `razzfazz-upgrade.sh` handles everything: pre-upgrade backups, `.env` migration, Docker image pulls, and health verification. Backups include encrypted `.env` files, full database dumps, and optionally model files — with a web UI for management and scheduled daily runs.

---

## 🆕 New Features

### Core Platform
- **Modular Docker Compose architecture** with profile-based service activation — enable only what you need
- **Centralized SMTP relay** (Postfix) for all services, configurable as relay or direct MX
- **Installation checksums & log snapshots** for governance and compliance (`razzfazz-checksum.sh`)
- **Non-interactive init** with package presets and explicit CLI options
- **Centralized application names** managed in `.env` for consistency across all services
- **Automated Getting Started PDF generation** with domain-specific content, installed model listing, and deployment metadata

### Identity & Access (Authentik)
- Fully automated Authentik blueprint provisioning for all 9+ services
- Google SSO integration with domain-restricted registration
- API key bypass for programmatic access without SSO
- Automatic admin super-group assignment
- Automatic blueprint upgrade on stack updates
- Enforced MFA and admin password rotation policies

### AI & LLM (GPUStack, Open WebUI, Dify)
- GPUStack operational modes: `standalone`, `master`, and `worker`
- Auto-detection of worker IP and hostname
- Custom Jinja chat templates for GPUStack models
- Qwen3.5 MoE support via upgraded llama.cpp backend
- Model sync service keeping Open WebUI in sync with GPUStack
- Dify upgraded to v1.13 with increased code execution array limits
- Custom Dify web frontend build with branding

### Infrastructure & Networking
- **Caddy** as sole entry point with automatic TLS (Let's Encrypt, self-signed, or custom certificates)
- Custom SSL certificate support with PEM validation
- Unauthenticated health check endpoints for monitoring
- GPUStack API key integration for Caddy authentication
- SSRF proxy isolation network for Dify sandbox
- Multipass VM scripts for rapid test environment provisioning (Linux & Windows)

### Backup & Recovery
- **Backup Manager Web UI** with file listing, sizes, delete, and restore
- Encrypted `.env` backup for sensitive configuration
- Option to include/exclude model files from backups
- Authentik volumes included in backup scope
- Database dump integration (pre-backup hooks)
- Configurable cron schedule (default: daily 03:00)

### Documentation & Help
- **Help Center** (`help.<domain>`) — self-hosted documentation hub with upstream mirroring and cache management
- **Setup Wizard UI** (`setup.<domain>`) — web-based configuration, secrets management, and checksum governance
- **License Overview** (`license.<domain>`) — auto-generated open-source license inventory
- **Getting Started PDF** — auto-generated, domain-customized quick reference with model inventory
- **Quick Start PDF** — compact A4 landscape version for printing

### Speech Services
- Architecture-aware Docker builds for Edge-TTS (amd64/arm64)
- Piper TTS German voice model (`jarvis-high`) pre-configured for Speaches

### Additional Services
- **Gitea** integration with SSO, SSH port mapping, and dedicated database
- **SearXNG** with German language defaults and curated search engine presets
- **Gotenberg** for PDF/document conversion in Dify workflows

---

## 🐛 Bug Fixes

### Stability & Reliability
- Fixed Dify 502 Bad Gateway errors during cold start
- Resolved GPUStack null content bug in CPU inference mode
- Fixed orphaned GPU process accumulation with background watchdog
- Stabilized worker-to-master connectivity in multi-node setups
- Eliminated restart loops caused by profile conflicts in test packages
- Increased PostgreSQL `max_connections` to 800 for high-concurrency workloads
- Increased Authentik session timeout for GPUStack to prevent premature logouts

### Security
- Sanitized credentials in example environment files
- Environment files permanently removed from git history
- Enforced MFA and admin password change on first login

### Configuration & Init
- Idempotent initialization scripts — safe to re-run
- Fixed conditional logic failures in init script
- Corrected hardware profile selection for multipass VMs
- Resolved SMTP relay configuration edge cases

### Reverse Proxy & TLS
- Fixed Caddy crash when `TLS_MODE` is empty
- Resolved PEM validation errors in custom certificate setup
- Refined health endpoint matcher to avoid false auth challenges
- Prevented crash on empty TLS mode variable

### Service-Specific
- Fixed SearXNG search engine defaults and language settings
- Resolved Gitea database connection and SSH port issues
- Fixed Dify plugin daemon Redis configuration
- Corrected model sync schema for OpenWebUI 0.8.8 compatibility
- Fixed Authentik avatar media path mapping and branding
- Resolved shell variable escaping in worker compose files
- Fixed DNS entry documentation: removed invalid `search.<domain>`, added missing `license.<domain>`

---

## 📊 By the Numbers

| Metric | Value |
|--------|-------|
| Total commits | 146 |
| Components touched | 18 |
| New services added | 7 (Help, Setup, Backup, License, Gitea, SearXNG, Gotenberg) |
| Docker Compose profiles | 10 |
| Authentik-managed apps | 9 |
| Integration test scenarios | 15+ (init) · 13+ (upgrade) |
| Supported TLS modes | 3 (Let's Encrypt, self-signed, custom certificate) |
| GPUStack deployment modes | 3 (standalone, master, worker) |

---

## 🔧 Infrastructure Requirements

- **OS:** Ubuntu 22.04+ / Debian 12+
- **Docker:** 24.0+ with Compose v2
- **RAM:** 16 GB minimum (32 GB recommended for LLM inference)
- **GPU:** Optional — AMD (Vulkan), NVIDIA (CUDA), or CPU-only
- **Storage:** 50 GB minimum (200 GB+ recommended with models)

---

## 🙏 Acknowledgments

This release represents the culmination of a focused engineering effort to make self-hosted AI infrastructure accessible, secure, and maintainable. Every component has been tested across multiple deployment scenarios — from single-box setups to distributed multi-worker clusters.

Built with ❤️ by the razzfazz.ai team.

---

*Full commit history: `git log --since="2026-01-01" --oneline`*
