# Authentik backup / export drop directory

This directory is a **local, operator-only** drop point for Authentik exports
(`ak export_blueprint` / full-backup dumps). It is intentionally kept empty in
the repository.

**Do not commit dump files here.** An Authentik full-backup dump serializes the
entire SSO object graph — user primary keys, token *identifiers*, group/role
mappings — and often captures whatever host and bastion IP addresses were in the
operator's environment at export time. Because `core/` is on the
`scripts/publish-public.sh` allow-list, anything committed here would ship in the
public release.

Two leftover dev dumps (Jan 2026) did exactly that — they carried a dev bastion
IP and bootstrap-token identifiers into the shippable surface.
They were removed under issue #27, and `.gitignore` in this directory now blocks
`*.yaml` / `*.yml` so the class cannot recur. The export gate in
`scripts/publish-public.sh` additionally fails **closed** on any Authentik
backup/credential-dump pattern or bare RFC1918 host IP in the staged surface.

If you need a real Authentik export, write it here for local use only — it will
be git-ignored and will never be published.
