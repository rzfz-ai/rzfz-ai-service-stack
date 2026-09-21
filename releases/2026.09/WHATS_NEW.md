# What's new — 2026.09

*The one thing that needs your attention before you upgrade is first.*

## ⚠️ Your API key changes — plan for it

If anything on your box talks to its **OpenAI-compatible API** — a script, a VS
Code agent, a CI job — that thing needs a **new API key** after the upgrade. The
URL you already use keeps working; the key you already use stops.

Why: the box is now fronted by the **LLM Manager** rather than by GPUStack
directly. One endpoint, whichever backend runs the model — and, for the first
time, usage reports and cost centres that can tell one consumer from another.
That only works with a key that identifies who is asking, which the old
GPUStack key never did.

**What to do:** after your box is on 2026.09, open
`https://llm.<your-domain>/` → **Keys**, mint one key per consumer, and swap it
in. About five minutes per client.

**If you are an operator:** tell your API users *before* you upgrade. They cannot
mint the new key until the box is upgraded, so an unannounced upgrade means every
API client fails at once with `401 invalid API key`.

→ [Migrate your OpenAI-compatible API access](../../docs/enterprise/how-to/migrate-openai-api-2026.09.md)

---

## Everything else in 2026.09

- **One LLM front door.** The LLM Manager is every box's inference front end now: one endpoint at `https://llm.<domain>/v1`, whichever backend holds the model, with its own console for deployments, workers, keys and a playground.
- **Switching a runner no longer means an outage** where the weights fit twice — the new engine starts before the old one retires, and the switch says in advance whether it will interrupt serving.
- **Wazuh**, new: file-integrity monitoring, log correlation and compliance mappings, with realtime host monitoring against a 12-hour scan cadence.
- **OpenUEM**, new: fleet inventory, software deployment and remote assistance, with its own PKI.
- **A model registry**, so fleet nodes pull models by digest — deduplicated, and air-gap friendly once seeded.
- **GPUStack runs non-root** by default on AMD boxes, and is now one backend behind the manager rather than the product itself.
- **Every module is acceptance-tested through a real browser** and made to do one thing, instead of being graded on container health.
- **Observability says what it sends** — transport and sampling are stated rather than inherited, and the module's own outbound analytics is off.
- **Vaultwarden and Stirling-PDF are stable** for this cycle.
- **The SIEM can reach a human.** Wazuh mails alerts of level 12 and above to the operator mailbox seeded at install; a box without a recipient says so.
- **Admins manage everyone's agents,** and a user may run several agents of one type — the tier decides how many.
- **An online upgrade verifies its images before it restarts** and refuses on a missing one, instead of reporting success and leaving a container that cannot be created.

Upgrading carries the cycle's environment migrations, adds five profiles and
seventeen images, and moves **nineteen existing images** forward — among them Open
WebUI 0.10 → 0.11 (a reorganised interface and a rebuilt streaming path) and Dify
1.16 → 1.17 (agent sandbox, skills, three database migrations). Every bump was
checked for a required `.env` step; none needs one, and the two conscious
non-steps are recorded in the release notes.

Full detail: [`RELEASE_NOTES.md`](RELEASE_NOTES.md).
