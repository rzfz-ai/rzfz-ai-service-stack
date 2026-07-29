# Release Notes — 2026.08

**Cycle status:** GA — latest `v2026.08-ga.2` (2026-07-29); `v2026.08-ga.1`
(2026-07-28); `v2026.08-ga` (2026-07-27). Shipped after a gated rc1→rc5 rollout
validated on the 0.91 reference box.

See `WHATS_NEW.md` for the cycle themes and `changelog.md` for the change list.
Per-tag notes: `releases/2026.08-ga.2/`, `releases/2026.08-ga.1/`, `releases/2026.08-ga/`.

## Patch — v2026.08-ga.2 (2026-07-29)
Four fixes found running ga.1 on production, plus one personal-agent feature. No
version bumps, no new modules, no schema or `.env` changes — a code-only rebuild of
the Start Portal, Configuration Portal, and Agent Manager images. **Start Portal
logout now fully signs you out** — it clears the forward-auth proxy session instead
of only the Authentik core session, fixing a broken half-logged-in state. The Config
Portal Release-Notes /
"What's new" dialog scrolls again (the mouse wheel now scrolls the dialog, not the
page behind it — the real fix was a background scroll-lock, plus per-version
stylesheet cache-busting; #216 follow-up); changing an agent type's memory limit no
longer returns a 500 (all JSON columns are serialized on save); and "Clone from
Gitea" states that the token needs both `read:user` and `read:repository` instead of
a bare 403. New: personal agents gain a **Restart** button and a Settings-page
**Update / Reinstall** control (#219), both non-destructive (chats/skills/files/config
preserved). Mode-B security review clean.

## Patch — v2026.08-ga.1 (2026-07-28)
Seven targeted fixes on the GA; no version bumps, no new modules, one new
(non-secret) `.env` key (`USER_DEFINED_APT_PACKAGES`, migration-tracked,
`requires_build`). Coding agents no longer die from PID exhaustion (#215); Dify
model-provider icons render again (#160); the Config Portal "What's new" dialog
scrolls on WebKit and the Network-Policy matrix headers are readable (#216/#217);
the coding-agent clone dialog takes a username and handles tokens correctly
(#213 — with its anti-exfil allowlist hardened to gate the username channel per
the release security review). Under the hood: a `USER_DEFINED_APT_PACKAGES` build
knob bakes system libs into per-user agent images (#212) and an init-suite
skip-pre-check (#214, test-only). Agent images are rebuilt on upgrade — re-provision
from **My Agents** after `rzfz post-install --refresh`.

## Cross-cutting themes
1. Offline / air-gapped operation (#184) — the flagship.
2. Apple-silicon Mac LLM gateway (#168/#169).
3. Enterprise documentation portal (#162/#163).
4. Customer-box delivery reliability (#171/#173/#159).
5. Stack-wide LLM observability (#193/#197).
6. Platform currency + security sweep (Dify/Authentik/Gitea/ClickHouse/Cognee/Onyx + base images; Valkey 9.1.1 RCE fix).

## GA hardening (rc4 → rc5)
Before the GA the full genuinely-open defect list was cleared and re-verified
end-to-end on 0.91: Vaultwarden SSO first login (#211 — deterministic
`email_verified` that also reaches upgraded boxes), domain-change SSO re-template
(#183), clean-install SSO host (#70), TLS-internal CA drift warning (#55), upgrade
`*_VERSION` reconcile (#177), Config-Portal orphan guard (#176), per-user agent
slug drift (#192), SearXNG verify false-negative (#201), plus the offline-upgrade
`images/` leftover (#209) and non-blocking help-cache warm (#210).

## Upgrade path
Standard `rzfz upgrade` from v2026.07-ga.10 (online or offline `--package`).
Pre-2026.07 boxes cross via the reorg-aware bootstrap. All cycle `.env`/image
changes apply automatically via the migration manifest (rc1→ga blocks); the
Authentik blueprint re-apply rides an `INIT_VERSION` bump (no image rebuild).
