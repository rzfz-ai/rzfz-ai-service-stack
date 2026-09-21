# Security Policy

This is the security policy for the rzfz.ai Service Stack. It says how to report a
vulnerability, which releases receive security fixes, and how a fix reaches your box.

It is the **policy**. The hardening instructions live in the
[README, section Security](README.md#-security).

## Reporting a vulnerability

Please report privately. **Do not open a public issue for a security finding.**

Two channels, both fine:

- **E-mail:** <security@rzfz.ai>
- **GitHub Private Vulnerability Reporting:** *Security → Report a vulnerability* on
  <https://github.com/rzfz-ai/rzfz-ai-service-stack>

A useful report contains:

- the exact version — the content of the `VERSION` file on the box, which `rzfz status`
  also prints;
- the affected module or profile (for example `chat`, `llm`, `monitor`), or the
  component if you can name it;
- reproduction steps, and what an attacker gains;
- whether the finding is reachable from outside the box or requires a local account.

What you can expect from us:

| Step | Commitment |
|---|---|
| Acknowledgement of your report | within **3 working days** |
| Initial assessment (severity, affected versions, planned handling) | within **10 working days** |

We will keep you updated while a fix is being prepared, and we will tell you plainly if we
decide not to fix something and why.

## Supported versions

Security fixes are produced for the **current GA cycle only**. We do not backport to the
previous cycle.

| Version | Security fixes |
|---|---|
| `2026.09-ga.N` (current GA cycle) | Yes |
| Any earlier cycle (`2026.08-ga.N` and older) | No |
| Pre-release / development builds | No |

The one supported path to a fix is an upgrade:

```bash
rzfz upgrade
```

If your box runs an older cycle, upgrade first. We will still want to hear about the
finding — but the fix ships in the current cycle, not as a patch to yours.

## Disclosure policy

We practise coordinated disclosure.

- We ask for **90 days** from your first report before public disclosure, or until a fixed
  release is out — whichever comes first. If a fix takes longer, we will say so and agree a
  new date with you rather than let the clock run out silently.
- We will **credit you by name or handle** in the release notes and in any advisory, if you
  want that. Say so in your report. If you prefer to stay anonymous, that is the default.
- Where an identifier applies, we request or reference a **CVE / GHSA** and name it in the
  advisory.
- **There is no bug bounty programme.** We do not pay for reports. We say this up front so
  nobody invests time expecting otherwise — the reporting channels above are open all the
  same, and we are grateful for every serious report.

## Scope

**In scope — what we build and ship:**

- the Compose topology and the service composition of the stack;
- the Caddy configuration and the routing/TLS surface it defines;
- the Authentik blueprints and the SSO/RBAC wiring that ships with them;
- the configuration portal;
- the LLM Manager;
- our own container images and the `rzfz` command-line tool;
- the shipped defaults — if a default setting is insecure, that is our bug.

**Out of scope — but still tell us:**

- **Upstream components** we package but do not author. Please report to that project as
  well, since the fix has to come from there. We want to know anyway, so we can pin,
  patch, or route around it in the next release.
- **Customer infrastructure** — your host OS, your network, your DNS, your identity
  provider, the physical machine.
- **Misconfiguration away from the shipped defaults.** If a change to the delivered
  configuration opens a hole, that is not a vulnerability in the stack; it is worth a
  documentation issue if our defaults or docs led you there.

Out of scope also means: no testing against boxes you do not own or operate, no denial of
service, no social engineering of operators or customers.

## How fixes ship

- A security fix ships as a regular patch release on the current cycle, `2026.MM-ga.N`.
- Every release body carries a fixed **Security** section. If there is nothing to report,
  it says so explicitly; it is never simply absent.
- Where a CVE or GHSA identifier exists, that section names it, together with the affected
  versions and the version that fixes it.
- Operators check their own box with:

  ```bash
  rzfz security-check
  ```

- Applying the fix is the ordinary upgrade path, `rzfz upgrade`.

## Hardening

The stack ships locked down by default: every service port binds to loopback, Caddy is the
only external entry point, and every UI sits behind SSO. A production deployment still has
work to do — rotating the delivered secrets, choosing a real TLS mode, enabling a host
firewall. That checklist, and the reasoning behind it, is the
[Security section of the README](README.md#-security). This document does not repeat it;
it tells you what to do when you find something we got wrong.
