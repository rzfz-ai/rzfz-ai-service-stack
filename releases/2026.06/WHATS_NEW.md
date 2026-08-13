# What's New — 2026.06 cycle

The 2026.06 cycle is about **operating the box with confidence**: upgrades that
explain and fix themselves, credentials you can manage without fear, and a box
that shrugs off a clock jump.

## Upgrades that explain themselves and fix themselves *(rc1)*

- 🔎 **Every upgrade keeps a journal.** If an upgrade goes wrong there's a precise
  record of which step broke and why — and a built-in diagnose-gate checks the
  things a healthcheck can't see (e.g. is the SSO outpost actually wired up?).
- 🩹 **Upgrades self-heal the common breakages.** "Server Error / Not found" on
  login pages, Authentik 500s after a version bump, a firewalled box that couldn't
  fetch a tool — now detected and repaired automatically mid-upgrade.
- 🆘 **Collect logs even when the UI is down** (`razzfazz-logs.sh take
  --standalone`), straight from Docker.
- 💾 **Backups stay complete + restorable** on boxes that used LightRAG.

## Credentials you can manage safely *(rc2)*

- 🔑 **One command to set an app admin password** — Authentik, Dify, Open WebUI,
  Gitea, GPUStack and more — per-app, all at once, or reset to the box default,
  applied to both the running service and your config.
- 🔁 **Rotate infrastructure secrets without bricking the box.** A tool that knows
  what's safe to touch: it auto-rotates the safe secrets, carefully re-keys shared
  ones with a health check and an undo recipe, warns before anything that would
  make old backups unreadable, and refuses the one key that has no recovery path.
- 💾 **Skip Dify plugin data in backups** with a new Backup setting.

## A box that survives a clock jump *(rc2)*

- ⏰ Boxes on virtual clocks could jump time far enough that the internal
  certificate looked expired and every page failed. The clock is now corrected in
  one step, so this can't happen. The docling UI also works under concurrent load,
  and Onyx no longer hogs the shared database.

## One model for everything, and document Q&A that works *(rc3)*

- 🧠 **qwen3.6 is now the default for chat, coding, and images alike** — one
  multimodal model with the full 1-million-token context for every session, so you
  don't pick a model per task.
- 📚 **Ask questions about your documents and get grounded answers** — the search,
  re-ranking, and chunking defaults that find the right passage now ship turned on.
- 🪶 **Lighter by default** — the extra models (Gemma 4, the heavy coding model)
  are downloaded and ready but kept switched off until you want them, so they don't
  tie up the GPU.

## Current, and verified end-to-end *(GA)*

- 🆙 **Component versions refreshed** across the stack ahead of GA.
- 🔐 **Full security review completed** — no release-introduced critical or high
  findings versus the previous release.
- ⤴️ **Upgrades validated both ways** — from the previous release and from the
  2026.04 baseline — so moving an existing box onto this release is a known-good
  path.

## Field fixes *(ga.2)*

- 🖥️ **Agent terminals reconnect after a reboot** — your per-user agents (Coding
  Tools, Hermes, …) come back with working terminals automatically.
- 📚 **Help Center shows the right GPUStack docs** for the LLM runtime your box
  actually runs, and "Stack documentation" in the Configuration Portal opens the
  Help Center cleanly.
- 💾 **Backups can't fill the OS disk** on boxes with a small system disk — a new
  optional `BACKUP_TMP_DIR` setting puts the backup's temporary build on your
  dedicated backup disk.
- 🧠 **Knowledge-base graph building works** on the default model again.
- 🧰 Smaller fixes: the hardware dashboard no longer lists a single GPU twice,
  and Coding Tools agents install the tool versions shipped in the release.

## Smoother upgrades and better document understanding *(ga.3)*

- ⤴️ **Upgrades just work, even on long-running boxes** — the upgrade now repairs
  empty per-service database logins automatically (which previously crash-looped
  document management and enterprise search), and no longer trips over files a
  container wrote into the stack folder as root.
- 📚 **Better answers from your documents** — the knowledge-graph, RAG and
  document-chat features now use a higher-capacity embedding model, so large
  document chunks are embedded correctly instead of being rejected.
- 💿 **Unattended USB installer** — a new bootable USB image installs the operating
  system and prepares the stack for first boot without manual steps.
- 🔐 **Audit-ready compliance tables** — the security documentation now publishes
  the full NIS2 and ISO 27001 control-mapping matrices a CISO/ISB can read directly.

## Safer upgrades on older installations *(ga.4)*

- 🛠️ **The ga.3 database-login repair is now careful.** ga.3's automatic repair of
  per-service database logins switched services to dedicated logins
  unconditionally, which could take Authentik, Open WebUI and Dify offline on
  older installations that share a single database administrator account. ga.4
  verifies the dedicated login works before switching, and otherwise keeps the
  working shared login — so the upgrade is safe on old and new boxes alike.
  **ga.4 is recommended over ga.3 for the upgrade path.**

## Customer handover, hardened *(ga.5)*

- 🔐 **Run your own security check** — `razzfazz-security-check.sh` gives you a
  posture + CVE report on your box any time (especially after a self-managed upgrade).
- 💾 **Backups encrypt correctly on fresh installs** (fixed a bug that left the
  backup password empty).
- ♻️ **Factory reset returns the box to delivery state** — a full reset restores
  the original sticker password.
- ✅ **Clearer day-1 handover** — the security checklist now walks through rotating
  *every* admin account, an in-place restore drill (no second machine), and
  verifying signups are closed.

---
*This is the General Availability release of the 2026.06 cycle (latest patch: ga.5).*

*Issued by razzfazz.ai GmbH - Member of SEQIS Group.*
