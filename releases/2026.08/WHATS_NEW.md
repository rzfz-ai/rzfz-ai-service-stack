# What's New — 2026.08

> **Patch v2026.08-ga.2 (2026-07-29):** Start Portal logout now fully signs you out
> (no more broken half-logged-in state); the Config Portal Release-Notes dialog
> scrolls again; changing a personal agent's memory limit saves without an error;
> "Clone from Gitea" tells you the token needs both `read:user` and `read:repository`.
> New: personal agents get a **Restart** button and a Settings-page **Update /
> Reinstall** control (#219) — both keep your chats, skills, files and config.

> **Patch v2026.08-ga.1 (2026-07-28):** coding agents stay alive under load — the
> PID-exhaustion crash is fixed (#215); Dify model-provider icons show up again
> (#160); the Config Portal "What's new" dialog scrolls on Safari and the
> Network-Policy matrix headers are readable (#216/#217); the coding-agent "Clone
> repository" dialog now takes a username and handles tokens correctly (#213).
> Per-user agent images gain a `USER_DEFINED_APT_PACKAGES` build knob (#212). After
> upgrading, re-provision your agents from **My Agents**.

The 2026.08 cycle centres on **running razzfazz.ai anywhere** — fully offline or
air-gapped, behind a corporate proxy, or fronting Apple-silicon Macs — with the
delivery, docs, and observability to support it. Six themes:

## 1. Offline / air-gapped operation (#184)

The flagship. A box now runs, upgrades, and enables modules with **zero internet
egress**. No runtime build or pull in any mode; a self-contained offline package
bundling every image, the model GGUFs, and the Enterprise overlay; hard
`verify-images` / `verify-models` readiness gates; local-GGUF model provisioning
plus a sideload-a-model page; three network modes (`online` / `proxied` /
`offline`) and an optional local registry-mirror; offline app-telemetry
suppression and an opt-in reversible host-hardening script; and an Offline/Network
status panel.

## 2. Run on Apple silicon — Mac LLM gateway (#168 / #169)

Front one or more Macs running Ollama as a second, load-balanced OpenAI endpoint
(GPUStack untouched), managed from a Configuration Portal panel with live health
and no-SSH model management.

## 3. Enterprise documentation portal (#162 / #163)

The gated docs publish to a Confluence-backed `docs.rzfz.ai`, with a
customer-facing "How to get support" page and an operator provisioning runbook.

## 4. Customer-box delivery reliability (#171 / #173 / #159)

Upgrades no longer hang on boxes that have accumulated backups; the current
security-architecture doc is delivered to every customer box; and the public
export drops internal build tooling.

## 5. Stack-wide LLM observability (#193 / #197)

An OTLP collector feeds OpenLIT for cost, latency, token, and prompt analysis
across the stack.

## 6. Platform currency + security

Dify 1.16.0 (CVE-2026-41948), Authentik 2026.5.5, Gitea 1.27.0, ClickHouse 25.8
LTS, Cognee 1.4.0, Onyx v4.3.9, and base-image refreshes — plus a GA-day
**Valkey 9.1.1** RCE fix (CVE-2026-56684 + CVE-2026-63639).

## Plus: a GA reliability pass

Before shipping, the cycle cleared its full open-defect backlog and re-verified
each fix end-to-end on the reference box — most visibly **Vaultwarden SSO first
login now works** (no more "verify your email" dead-end, #211), a **domain change
no longer breaks SSO** (#183), and **upgrades reconcile stale version pins and no
longer leave multi-GB offline payloads behind** (#177 / #209).
