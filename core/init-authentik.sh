#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
set -e

echo "Starting Authentik Initialization..."
echo "Scenario detected: ${DEPLOYMENT_SCENARIO}"

# ==============================================================================
# INIT FLAG FILE - Ensures this script only runs ONCE per installation
# ==============================================================================
# The flag file is stored in the persistent authentik-media volume.
# To force re-initialization (e.g., after a fresh install), delete this file
# or remove the authentik-media volume.
# ==============================================================================
INIT_FLAG_FILE="/data/media/.authentik_initialized"
INIT_VERSION="4.22"  # Bump this to force re-run (4.22: the Entra and Google SSO blueprints gain the link-by-e-mail check — a first source sign-in that matches an existing account by e-mail links only when the IdP vouches for the address (Entra: the ID token's xms_edov claim, read by the new "razzfazz.ai - E-mail link proof (Entra)" mapping; Google: verified_email plus hd equal to the domain) and the domain is on the allowed list (ENTRA_OAUTH_DOMAIN / GOOGLE_OAUTH_DOMAIN); the check is bound on default-source-authentication, whose policy engine mode becomes `all`. REQUIRED bump: the SSO blueprints are copied only on the full path. (Merged on top of the OpenHands removal, which took 4.21 on main.) 4.21: 2026.11 — the OpenHands module is removed: 02-groups.yaml loses "razzfazz.ai OpenHands Users" and 23-openhands.yaml leaves the base set and the profile-toggle re-copy list. REQUIRED bump: 02 is in the pinned base set, and the full path wipes the rendered 23-openhands.yaml from the Authentik volume; apply-policy-bindings.py deletes the application, provider, group and blueprint instance the module had created. 4.20: #2927 — google/11-policies.yaml renders the allowed Google Workspace domains from GOOGLE_OAUTH_DOMAIN (a list; empty admits nobody) instead of the hard-coded company domain; the SSO blueprints are copied only on the full path, so only the bump delivers the policy to an installed box. 4.19: 07-mfa-policy.yaml documents the admin-MFA default as `false` (enforcement is switched on after the handover; apply-policy-bindings.py reads the same default); REQUIRED bump: 07 is in the pinned base set. 4.18: #2816 — 04-outposts.yaml no longer lists `providers:` (a re-apply replaced the outpost's provider set with the 9 core ones and detached every module provider); REQUIRED bump: 04 is in the pinned base set, so only the bump delivers it. 4.17: #2473 D6 — 06-oauth-groups-scope.yaml gains the "razzfazz.ai OAuth Mapping: knowledge scopes" scope mapping (claim rzfz_kb_groups) and the openwebui-oidc provider attaches it; REQUIRED bump: 06 is in the re-copy list and the openwebui-oidc provider is copied only on the full path. 4.16: #2473 D4 — new pinned base blueprint 08-upstream-group-sync.yaml (the "razzfazz.ai - Reconcile upstream groups" policy on the source login stage, the 8 h source session cap) and the Entra/Google "Upstream groups" source mappings, their scopes (Graph, Cloud Identity) and enrollment-login bindings; REQUIRED bump: 08 is in the pinned base set, and the SSO blueprints are copied only on the full path. 4.15: #2476 — new base blueprint 38-openbao.yaml, dual-layer like 37-wazuh: the forward-auth ProxyProvider "Caddy Forward Auth Provider for OpenBao" on the group-bound application `openbao` (bao.<domain>, which Caddy already forward_auths) and the OAuth2Provider "OpenBao OIDC Provider" (sub_mode hashed_user_id, the groups mapping) on the hidden, also bound application `openbao-oidc` for OpenBao's own OIDC sign-in. In the profile-toggle re-copy list and deployed only with the openbao profile and OPENBAO_CLIENT_SECRET, OPENBAO_OIDC_CLIENT_ID and OPENBAO_OIDC_CLIENT_SECRET set (gate_openbao_blueprint). REQUIRED bump: a box that upgrades before enabling the opt-in profile would otherwise never receive the file. 4.14: #2473 — 02-groups.yaml gains "razzfazz.ai Auditors" and "razzfazz.ai Staging Canary"; REQUIRED bump: 02 is pinned. 4.13: #2480 — 01-brand.yaml logo/favicon move to the rzfz.ai SVGs; REQUIRED bump: 01 is pinned. 4.12: #2551 — 07-mfa-policy.yaml defines a dedicated admin MFA validation stage (configure, with TOTP/WebAuthn setup stages) and a no-device policy, and its admin policy matches both admin groups; Authentik's own MFA stage is left alone; REQUIRED bump: 07 is in the pinned base set. 4.11: #2522 — the google/ and entra/ SSO blueprints shared their bare file names in /blueprints, so with both enabled Entra overwrote Google (prod: "Google - Flows" applied Entra content); they are now deployed as google-<file> / entra-<file>, and migrate_sso_blueprint_instances repoints the existing instances onto the new names before the files appear. Rides the same 4.11 full path as #2501 (same release); no SSO source file changed, so the `sso` pin is unchanged. #2501 — three blueprint instances sat in `error` on prod since the 2026.08 Authentik bump. (a) google/12-flows.yaml + entra/12-flows.yaml identified their domain-restriction PolicyBinding by `target: !Find [authentik_flows.flow, …]`, which returns the flow_uuid, while a binding's target is the flow's pbm_uuid: the lookup never matched, the create hit unique_together (policy, target, order) and rolled the blueprint back on every apply. Now `target: !KeyOf` + `order` in the identifiers (the model's unique key). REQUIRED bump: the SSO blueprints are copied ONLY on the full path, so without it the fixed file never reaches an installed box (the .init-version-pin `sso` line now guards this). (b) 18-synapse-oidc.yaml is deployed only with the matrix profile and both credentials (gate_synapse_blueprint) — a blank client_id otherwise. (c) repair_managed_scope_mappings puts a NULL `managed` key back on Authentik's built-in scope mappings (prod: OpenID 'email'), so Authentik's own "System - OAuth2 Provider - Scopes" blueprint resolves the row instead of colliding on its name; runs on every path before the reconcile. 4.10: #2416 — 4.10: #2416 — 01-brand.yaml branding_title "razzfazz.ai - local matters" → "rzfz.ai - local matters" (the rebrand; the start portal's exact <title>; razzfazz.ai stays the legal entity). REQUIRED bump: 01-brand.yaml is in the pinned base set (not in the profile-toggle re-copy list), so an upgraded box keeps the old brand row on every login/consent/admin page without it (measured on 0.208 at 2026.09-ga.1, 2026-09-23). 4.9: #2247 — every razzfazz.ai group in 02-groups.yaml gains the attribute goauthentik.io/user/token-maximum-lifetime = days=30 (operator decision 2026-09-18, Jira RZFZAI-1971): without it Authentik clamps every API token to default_token_duration (30 min) on every write. REQUIRED bump: 02-groups.yaml is NOT in the profile-toggle re-copy list (the 4.5/4.7 lesson), so an upgraded box would keep the attribute-less groups and the fix would have no delivery path. 4.8: #1982 — 37-wazuh.yaml gains the "razzfazz.ai OAuth Mapping: wazuh profile" scopemapping and the wazuh-oidc provider swaps authentik's default 'profile' mapping for it, so a Super Admin reaches the Wazuh API as `admin` and matches Wazuh's own shipped wui_opensearch_admin rule (measured: akadmin -> rbac_roles=[] and 0 agents; admin -> rbac_roles=[1] and 2 agents). REQUIRED bump: 37-wazuh.yaml IS in the profile-toggle re-copy list, and a toggle is the only other delivery path — a box that never toggles the wazuh profile would keep the old provider mapping and its console would go on reporting "no agents registered" while the alerts from the indexer make it look healthy. 4.7: #1635 — the three agent quota-tier groups (agent-basic / agent-power / agent-admin) join 02-groups.yaml, names verbatim from catalog.py::SEED_TIERS because Database.resolve_user_tier matches `quota_tiers.id IN (<the user's groups>)` exactly. Nothing had ever created them — no blueprint, no init, no migration — so on every box a normal user resolved to NO tier and all seven agent cards read "Restricted — Contact admin for access"; only Super Admins got through, via the _UNLIMITED_ADMIN_GROUPS escape hatch that hid the defect from everyone who sets a box up. REQUIRED bump: 02-groups.yaml is NOT in the profile-toggle re-copy list, so without it an upgraded box never receives the groups and the fix has no delivery path (the 4.5 lesson). 4.6: #1444 cutover C4 / operator decision D3 — llm.<domain> is the LLM Manager: 33-llm-manager-ui.yaml external_host is https://${LLM_DOMAIN} (new .env key, default llm.${MAIN_DOMAIN}; env-changes.json adds it), GPUStack's proxy app follows GPUSTACK_DOMAIN whose default moved to gpustack.${MAIN_DOMAIN} (env-changes change_default). REQUIRED bump: with INIT_VERSION/profiles/domain unchanged the fast path skips the re-template, the embedded outpost would keep matching the OLD hosts and 404 every request on the new names while Caddy already routes them (the #1200 precedent). Caddy keeps llm-manager.<domain> as a 301 for the console paths for one cycle. 4.5: #1310 rev-B / PR #1349 — two BASE blueprints changed and neither is in the profile-toggle re-copy list: 04-outposts.yaml lost the dead "Caddy Forward Auth Provider for Setup" !Find (the provider left with the setup UI in 2026.07, #22; !Find→None failed the whole outpost blueprint on every box), 06-access-policies.yaml went from a bare `entries:` (YAML null, importer rejects) to `entries: []`. REQUIRED bump: measured on 0.79 after the merge — a wazuh profile toggle re-copied the module blueprints and left both base files at their old content, so the fix had no delivery path to an installed box. The pin in core/Authentik/blueprints/base/.init-version-pin now ties INIT_VERSION to the content of the non-re-copied base blueprints (test_1310_init_version_follows_base_blueprints.py). 4.4: #1200 + #1203, one bump for two base blueprints. #1200: the Settings portal moves from config.<domain> to settings.<domain> — 25-config.yaml's ProxyProvider external_host is now https://${SETTINGS_DOMAIN} (new .env key, default settings.${MAIN_DOMAIN}; env-changes.json adds it on upgrade, Caddy serves the portal on {$SETTINGS_DOMAIN} and keeps {$CONFIG_DOMAIN} as a 301 for one cycle). REQUIRED bump: with INIT_VERSION/profiles/domain unchanged the fast path below skips the blueprint re-template, so the provider would keep external_host=config.<domain>; the embedded outpost matches the forward_auth Host against external_host and would 404 every request on the new name (the exact OpenUEM/4.1 and Wazuh rev-A failure mode) while Caddy already routes it. #1203: 01-brand.yaml branding_default_flow_background flips from the 5.85 MB razzfazz.png to the 174 KB razzfazz.webp (#526 asset; verified rendering on 0.91 via the API hotfix + render-cache clear). Bare filename, NOT an absolute path — the 3.8 lesson holds. Bump forces the brand re-apply so every fleet/customer box stops serving the PNG on the login page. 25-config.yaml was already in the profile-toggle re-copy list; 01-brand.yaml carries no profile-dependent value, the version bump alone delivers it. 4.3: #855 Wazuh module — new base blueprint 37-wazuh.yaml, DUAL-LAYER like Gitea (rev-B blocker 4): a forward_single ProxyProvider "Caddy Forward Auth Provider for Wazuh" (external_host https://${WAZUH_DOMAIN}) on the group-bound application slug `wazuh`, PLUS the OAuth2Provider "Wazuh OIDC Provider" on the hidden application slug `wazuh-oidc` for the dashboard's own native SSO, plus the inline group "razzfazz.ai Wazuh Users". Added to the profile-toggle re-copy list; apply-policy-bindings.py binds `wazuh` to Wazuh Users / Super Admins and attaches the proxy provider to the embedded outpost in its attach-ALL loop. The OAuth2Provider ALONE was the rev-A defect: OAuth2 providers do not attach to the outpost, so Caddy's domain-wide forward_auth on wazuh.<domain> matched nothing and 404'd every user before the login redirect. REQUIRED bump: the fast path below skips the blueprint copy entirely when INIT_VERSION/profiles/domain are unchanged, so a box that upgrades BEFORE enabling the opt-in wazuh profile would never get the Authentik objects and the SSO app would silently not exist. Needs WAZUH_CLIENT_SECRET (proxy) and WAZUH_OIDC_CLIENT_ID/WAZUH_OIDC_CLIENT_SECRET (native OIDC) in .env — two separate secrets, the GITEA_CLIENT_SECRET/GITEA_OIDC_CLIENT_SECRET split (init.sh generates them; upgrade.sh _gen_if_empty backfills). Blueprint prefix is 37, not the plan's 35: 35-openuem.yaml and 36-searxng.yaml were taken. 4.2: #1157/#1158 rev-B — new base blueprint 36-searxng.yaml ("Caddy Forward Auth Provider for SearXNG" ProxyProvider + `searxng` application for search.<domain>/${SEARXNG_DOMAIN}) added to the fast-path re-copy list. REQUIRED bump: with INIT_VERSION/profiles/domain unchanged the fast path below skips the blueprint copy entirely, so the file alone would never reach an already-installed box on `rzfz upgrade` — the SSO app would silently not exist while the new {$SEARXNG_DOMAIN} Caddy block already forward_auths against it (dead tile on every upgraded box; the exact OpenUEM/4.1 failure mode, caught in the #1158 review). Needs SEARXNG_CLIENT_SECRET in .env (init.sh generates it; upgrade.sh _gen_if_empty backfills it). The v5 group renames themselves need no bump — upgrade.sh's Django migration covers those. 4.1: #1075 OpenUEM — new base blueprint 35-openuem.yaml ("Caddy Forward Auth Provider for OpenUEM" ProxyProvider + `openuem` application for openuem.<domain>), new 02-groups.yaml entry "razzfazz.ai OpenUEM Users", and apply-policy-bindings.py binds the app to that group + Super Admins. REQUIRED bump: with INIT_VERSION/profiles/domain unchanged the fast path below skips the blueprint copy entirely, so the files alone would never reach an already-installed box on `rzfz upgrade` — the SSO app would silently not exist exactly where an operator enables an opt-in module. Needs OPENUEM_CLIENT_SECRET in .env (init.sh generates it; upgrade.sh _gen_if_empty backfills it). 4.0: #843 LLM tier groups + llm-manager bindings — new base blueprint 02-groups.yaml entries "razzfazz.ai LLM Admins" / "razzfazz.ai LLM Users" (names MUST match config.py's LLM_MANAGER_ADMIN_TIER_GROUPS/LLM_MANAGER_USER_TIER_GROUPS defaults verbatim), plus apply-policy-bindings.py binds the `llm-manager` app (33-llm-manager-ui.yaml, previously unbound) to LLM Users/LLM Admins/Super Admins. Without this, #842's three-tier require_role() gates in the LLM Manager console are unreachable by anyone but a Super Admin — the two tiers exist in code but no Authentik group/binding puts anyone in them. Bump forces re-template + re-apply so the groups + binding land on upgrade. 3.9: #386 F-P4-12 — the Config Portal ProxyProvider drops the LAST hardcoded client_secret ("config_app_client_secret_static", published on the public mirror) for the generated ${CONFIG_CLIENT_SECRET}; bump forces the blueprint re-apply so existing boxes replace the publicly-known value. Needs CONFIG_CLIENT_SECRET in .env (init.sh generates it; upgrade.sh _gen_if_empty backfills it). 3.8: REVERT of 3.7 — the Brand serializer rejects absolute paths ("Absolute paths are not allowed"), so the 3.7 value "/media/public/razzfazz.png" put 01-brand.yaml into BlueprintInstance status=error on every box that applied it (no brand/CSS updates land while errored). Back to the bare "razzfazz.png": authentik 2026.5.x resolves it under media/public/ and emits a signed /files/ URL that verifiably renders (the original #544 "bare name never resolves" diagnosis does not hold on 2026.5.x). Bump forces re-apply to clear the errored instance. 3.7: #544 — branding_default_flow_background must be the SERVED MEDIA PATH (/media/public/razzfazz.png), not a bare filename; the field is emitted verbatim as the flow-background URL, so the bare name never resolved and every box served Authentik's stock background since day one. Bump forces the brand blueprint re-apply so the branding finally lands on existing installs. 3.6: #254 Phase-3 — new base blueprint 33-llm-manager-ui.yaml adds the "Caddy Forward Auth Provider for LLM Manager" ProxyProvider + "LLM Manager" application (slug llm-manager) for llm-manager.<domain>; init-authentik auto-attaches it to the embedded outpost. Needs LLM_MANAGER_UI_CLIENT_SECRET in .env. Bump forces re-apply so the provider lands (turns the /api,/ui 404 into a real SSO login for the manager console). 3.5: ga.10 #OIDC-grant-types — set grant_types=[authorization_code,refresh_token] on the 4 native-OIDC providers (openwebui-oidc, gitea-oidc, 18-synapse-oidc, 20-vaultwarden). Authentik 2026.5.x added a REQUIRED grant_types list; blueprints that omit it leave it [] and authorize.py rejects every SSO authorize as "invalid_request/malformed" before the policy check → Chat/Gitea/Matrix/Vaultwarden SSO all broke on ga.9. Bump forces re-apply so the fix lands on upgrade. 3.4: #211 Vaultwarden SSO email_verified — new BASE blueprint 07-oauth-email-verified-scope.yaml declares the `razzfazz.ai OAuth Mapping: email_verified` scopemapping (scope_name `email`, returns email_verified=true); 20-vaultwarden.yaml attaches it to the Vaultwarden OIDC provider AFTER the default email mapping so it wins on that key, unblocking Vaultwarden 1.36.x SSO first login ("verify your email" dead-end / Finding 10). REQUIRED bump: the blueprint files alone do NOT reach an already-installed box on `rzfz upgrade` — INIT_VERSION/profiles/domain were unchanged so the fast-path below skips blueprint copy; bumping the version forces the full re-template + re-apply so the mapping + provider attachment actually land on upgrade. 3.3: #33 True SSO group->role mapping — new base blueprint 06-oauth-groups-scope.yaml declares the `razzfazz.ai OAuth Mapping: groups` scopemapping (emits the user's Authentik group names as a `groups` claim); the 4 native-OIDC providers (openwebui-oidc, gitea-oidc, 18-synapse-oidc, 20-vaultwarden) now attach it so Open WebUI (ENABLE_OAUTH_ROLE_MANAGEMENT + OAUTH_ADMIN_ROLES=SSO_ADMIN_GROUP) and Gitea (add-oauth/update-oauth --admin-group SSO_ADMIN_GROUP --group-claim-name groups) can map an Authentik admin group -> the app admin role. Synapse/Vaultwarden emit the claim but can't consume it for admin-role in stable configs (documented). Version bump forces existing boxes to re-apply blueprints so the groups scope mapping + provider property_mappings land. 3.2: #36 / PR #84 C1 — per-instance forward-auth for the coding-agent subdomains (opencode-<token>.agents.<domain>, …). agent-manager now registers a per-instance `forward_single` Authentik provider + application at PROVISION time and deregisters at stop/delete (app/services/authentik_client.py, via the Authentik REST API with AUTHENTIK_BOOTSTRAP_TOKEN), so the embedded outpost authenticates each instance host with the WELL-TESTED forward_single mode every other app uses. A single domain-level forward_domain provider was tried first but LOOPED on the post-login callback (~8 redirects → HTTP 400); it is intentionally NOT in the blueprint. This re-apply also removes any stale "Caddy Forward Auth Provider for Agent Instances" forward_domain provider + `agent-instances` app left by an interim build. 3.1: #36/#61 — UNIFIED "MCP & Agent Manager". The my-agents Application (28-my-agents.yaml) is renamed "My Agents" → "MCP & Agent Manager" (single combined tile + app); its icon fixed from the never-existed razzfazz-ai_agent_icon.png to razzfazz-ai_mcp_icon.png (new asset). The standalone MCP Manager app (31-mcp-manager.yaml, slug `mcp`) is retired as a user-facing app → renamed "MCP Manager (backend)", kept ONLY as hidden/tile-less backend forward-auth for mcp.${MAIN_DOMAIN}. apply-policy-bindings.py now binds slug `mcp` to "razzfazz.ai AI Agents Users" + Super Admins so mcp.<domain> is gated by the SAME single group (no-op when the mcp profile is inactive). 3.0: #36 — new 31-mcp-manager.yaml blueprint (Personal MCP Manager forward-auth proxy provider + `mcp` application for mcp.${MAIN_DOMAIN}); added to the fast-path re-copy list; provider auto-attached to the embedded outpost by apply-policy-bindings.py. 2.9: SSO flow-integration unified — google+entra now share ONE generated z-15-sso-flow-integration.yaml that lists all enabled OAuth sources together. The identification-stage `sources` attr is a SET (blueprint attrs overwrite, not append), so the old per-provider 14-flow-integration.yaml files clobbered each other and enabling Entra dropped Google off the login page. Legacy z-14-*-flow-integration.yaml are removed at init. Also: start-portal logout now hits the forward-auth outpost sign_out so the proxy session is cleared (was looping back to a cached portal). 2.8: B-11 — admin-MFA enforcement default. New blueprint 07-mfa-policy.yaml declares the `razzfazz.ai - Require Admin MFA` expression policy; apply-policy-bindings.py grew ensure_mfa_admin_binding() that wires it to the default-authentication-flow MFA-validation flowstagebinding. Honours RAZZFAZZ_REQUIRE_ADMIN_MFA env var (default `true`, NIS2 21(2)(j) posture). 2.7: M028 S05-REDO Option A — every per-module Application now hard-codes meta_launch_url=blank://blank in the blueprint (Authentik frontend's appHasLaunchUrl filter rejects non-http(s) URLs, hiding the tile from the user library while leaving the Application + ProxyProvider intact for forward-auth). The state: ${OPENHANDS_BLUEPRINT_STATE} / state: ${CRAWL4AI_BLUEPRINT_STATE} machinery retires (apps stay always-present); start app's Super-Admins-only PolicyBinding wiped so every authenticated user can be redirected there post-login. Per-profile *_LAUNCH_URL exports are now dead (blueprints no longer reference them) but kept for one cycle pending M025 hygiene sweep. 2.6: M028 S05 REVERT — restored Application entries; 2.5: M028 S05 (REVERTED) App-library slim-down by deletion; 2.4: M028 start portal scaffold; 2.3: apply-policy-bindings extracted; 2.2: blueprint state gating; 2.1: per-module Authentik groups; 2.0: icon URLs absolute; 1.9: redirect_uris list-of-dicts; 1.8: profile-gating fixes)

# rc6.7 #94: shared by both fast-path and full-path. Must be defined BEFORE
# the fast-path block (which exits early). The bindings python lives at
# /init/apply-policy-bindings.py (mounted from core/Authentik/) and is
# docker-cp'd into authentik-worker for execution there (worker has the
# Authentik Django ORM available).
# #147: the init container is bare alpine, so it installs docker-cli (to reach
# authentik-worker via the socket-proxy) + gettext (envsubst for blueprint
# templating) at RUNTIME. On a restricted-network box that apk FAILS — and it
# used to fail SILENTLY (>/dev/null 2>&1 on one path, and the always-run
# reconcile path below never installed at all), so outpost bindings were never
# reconciled → gated apps 500/404 (the MEGAS case). ensure_init_tools is
# idempotent (skips if already present) and FAILS LOUD with the host-side
# remediation, so the failure lands in the upgrade journal instead of vanishing.
ensure_init_tools() {
    if command -v docker >/dev/null 2>&1 && command -v envsubst >/dev/null 2>&1; then
        return 0
    fi
    echo "Installing init tools (gettext, docker-cli)..."
    if ! apk add --no-cache gettext docker-cli; then
        echo "ERROR(#147): apk add gettext/docker-cli FAILED — restricted network? The init" >&2
        echo "  container cannot reconcile outpost bindings or template blueprints. Run the" >&2
        echo "  host-side backstop:  rzfz post-install --refresh   (reconcile_authentik_bindings)" >&2
        echo "  or directly:  docker cp core/Authentik/apply-policy-bindings.py authentik-worker:/tmp/apb.py && docker exec -e COMPOSE_PROFILES=\"\$COMPOSE_PROFILES\" -e RAZZFAZZ_REQUIRE_ADMIN_MFA=\"\$RAZZFAZZ_REQUIRE_ADMIN_MFA\" -e RAZZFAZZ_ACCESS_GROUP=\"\$RAZZFAZZ_ACCESS_GROUP\" authentik-worker python /tmp/apb.py" >&2
        return 1
    fi
    command -v docker >/dev/null 2>&1 || { echo "ERROR(#147): docker still missing after apk add." >&2; return 1; }
}

# copy_media_assets — put the branding assets where the servers read them from.
#
# #526: this MUST run on the version-gate fast path too, not only on a full
# re-init. The media copy used to live inline below the gate, so an upgrade that
# did not bump INIT_VERSION never executed it. Ship a new asset (the 174 KB
# razzfazz.webp that replaced the 5.85 MB PNG) and repoint the stylesheets, and
# an upgraded box gets CSS pointing at `/branding/media/razzfazz.webp` while the
# file is never placed under /data/media/public/ — Caddy 404s it on all five
# surfaces and the background silently vanishes. Verified on 0.236 (ga.12):
# razzfazz.png present and served 200/5.85 MB, razzfazz.webp 404.
#
# Same reasoning, and the same fix, as apply_policy_bindings above: the work is
# idempotent, so always doing it is cheaper than reasoning about when to skip it.
copy_media_assets() {
    SOURCE_MEDIA="${SOURCE_MEDIA:-/init/media}"
    TARGET_MEDIA="${TARGET_MEDIA:-/data/media}"
    TARGET_PUBLIC="${TARGET_PUBLIC:-/data/media/public}"
    TARGET_BLUEPRINTS="${TARGET_BLUEPRINTS:-/blueprints}"
    # #1178 defense-in-depth: the authentik image ships /data/media as a
    # symlink -> /media, and Docker's copy-on-first-use plants that link in a
    # fresh volume. Writing "into" it lands in this container's ephemeral
    # layer (branding 404, init flag never persists). authentik-media-migrator
    # replaces the link before we start; if that ever regresses, catch it here
    # too. `-L` only — a real directory is the box's data and stays.
    if [ -L "$TARGET_MEDIA" ]; then
        echo "WARN: $TARGET_MEDIA is a symlink ($(readlink "$TARGET_MEDIA")) — replacing with a real directory (#1178)"
        rm "$TARGET_MEDIA"
    fi
    mkdir -p "$TARGET_MEDIA" "$TARGET_PUBLIC"

    echo "Copying media assets..."
    if [ ! -d "$SOURCE_MEDIA" ]; then
        echo "Media source directory not found."
        return 0
    fi
    cp -r "$SOURCE_MEDIA"/* "$TARGET_MEDIA"/ || echo "No media files found."
    # Copy branding + icon assets to /data/media/public/ where Authentik's
    # FileBackend and Caddy's /branding route both read them.
    #
    # #526 *.webp is in this list because the glob is the ONLY thing that puts an
    # asset where Caddy's /branding route reads it from
    # (root /srv/authentik-media/media/public). An asset referenced by CSS but
    # missing from this glob lands in /data/media/ and 404s on every surface —
    # silently, since a missing background is just a plain page. Adding a format
    # here is part of adding a format, not an afterthought;
    # tests/unit/branding/test_background_asset.py fails if a referenced
    # extension is not copied.
    for f in "$TARGET_MEDIA"/*.png "$TARGET_MEDIA"/*.svg "$TARGET_MEDIA"/*.webp; do
        [ -f "$f" ] && cp "$f" "$TARGET_PUBLIC"/ 2>/dev/null
    done
    # Copy media to blueprints folder for blueprint processing
    cp -r "$SOURCE_MEDIA"/* "$TARGET_BLUEPRINTS"/ \
        || echo "Failed to copy media to blueprints."
    # PR review R2: ownership rides WITH the copy. The full path used to chown
    # after calling this function, so the fast path (which only calls this)
    # left a newly-shipped asset root-owned — Caddy serves 644 fine, but
    # Authentik's FileBackend runs as uid 1000 and cannot later rewrite the
    # tree (admin-UI icon upload). Best-effort: in the container init runs as
    # root and this succeeds; the non-root bash test harness cannot chown and
    # world-read is already enough to serve.
    chown -R 1000:1000 "$TARGET_MEDIA" 2>/dev/null \
        || echo "WARN: could not chown media to 1000:1000 (non-root run?)"
    chmod -R 755 "$TARGET_MEDIA" 2>/dev/null || true
}

# #1148 — the Authentik superuser is `rzfz-admin` on NEW installations.
#
# Authentik knows no bootstrap USERNAME (only …_PASSWORD/_TOKEN/_EMAIL), so the
# initial superuser is always created as `akadmin`. The username is a plain
# mutable field afterwards and the primary key survives a rename, so every OIDC
# link (which maps by sub / e-mail) stays intact.
#
# Idempotent and safe on an existing box BY CONSTRUCTION: `.env.example` ships
# `rzfz-admin`, while the migration writes `akadmin` for a box that already has
# one — so on an upgrade the target equals the bootstrap name and this is a
# no-op. It also refuses when the target name is already taken by someone else.
# Never fatal: a box whose superuser could not be renamed still works, it just
# keeps the old name.
rename_bootstrap_admin() {
    local target="${RAZZFAZZ_ADMIN_USERNAME:-akadmin}"
    if [ -z "$target" ] || [ "$target" = "akadmin" ]; then
        echo "Authentik superuser stays 'akadmin' (RAZZFAZZ_ADMIN_USERNAME='${target}') — no rename."
        return 0
    fi
    if ! ensure_init_tools; then
        echo "Skipping the superuser rename — init tools unavailable (see ERROR above)."
        return 0
    fi
    # #2010: two things this line got wrong, both measured on 0.79 2026-09-12.
    #
    # 1. DJANGO_SETTINGS_MODULE. `django.setup()` does not CHOOSE a settings
    #    module; a bare `python -c` in that container has none, so the exec died
    #    with "Requested setting LOGGING_CONFIG, but settings are not
    #    configured". With the variable supplied the same snippet works.
    #
    # 2. `set -e` versus a captured assignment. `_rename_out=$(...)` that fails
    #    ends the script AT THAT LINE, so the `_rename_rc=$?` written to handle
    #    it was unreachable — and the output was captured, so Django's
    #    explanation went into a variable nobody printed. The install stopped
    #    with `INCOMPLETE` and not one word of cause. Same contradiction as
    #    #2007: a failure the code is written to tolerate, made fatal and
    #    invisible by how it is invoked. `if ... then/else` keeps the status
    #    readable in POSIX sh (this file runs under BusyBox ash — see the
    #    PIPESTATUS note below for why that matters).
    if _rename_out=$(docker exec \
            -e RZFZ_TARGET_USERNAME="$target" \
            -e DJANGO_SETTINGS_MODULE="${AUTHENTIK_DJANGO_SETTINGS:-authentik.root.settings}" \
            authentik-worker python -c '
import os
import django
django.setup()
from authentik.core.models import User
target = os.environ["RZFZ_TARGET_USERNAME"]
if User.objects.filter(username=target).exists():
    print(f"Authentik superuser {target!r} already exists — nothing to rename.")
elif not User.objects.filter(username="akadmin").exists():
    print("No akadmin user — nothing to rename (already renamed, or not bootstrapped yet).")
else:
    u = User.objects.get(username="akadmin")
    u.username = target
    u.save(update_fields=["username"])
    print(f"Renamed the Authentik superuser akadmin -> {target} (pk {u.pk} unchanged, #1148)")
# #2644: Authentik re-applies its own system/bootstrap.yaml, which looks the
# superuser up by its context username (akadmin) and the bootstrap token by
# identifier AND user. After the rename that lookup creates a second akadmin
# and the token create collides on its unique identifier: the instance goes to
# status=error on every re-apply (the reconfigure scenario printed it). Point
# the instance context at the renamed superuser, so the re-apply finds the
# existing user and token and changes nothing.
# Review I3: this runs on every authentik-init, upgrades included, so it writes
# ONLY when the bootstrap token exists and belongs to that very user, and that
# user is a superuser. Anything else is left exactly as it is. Its own failure
# is reported as its own, never as a failed rename.
try:
    from authentik.blueprints.models import BlueprintInstance
    from authentik.core.models import Token
    owner = User.objects.filter(username=target).first()
    tok = Token.objects.filter(identifier="authentik-bootstrap-token").first()
    for bp in BlueprintInstance.objects.filter(path="system/bootstrap.yaml"):
        ctx = dict(bp.context or {})
        if ctx.get("username") == target:
            continue
        if owner is None or not getattr(owner, "is_superuser", False):
            print(f"Bootstrap blueprint left as it is: {target!r} is not a superuser (#2644).")
        elif tok is None or tok.user_id != owner.pk:
            print(f"Bootstrap blueprint left as it is: the bootstrap token does not belong to {target!r} (#2644).")
        else:
            ctx["username"] = target
            bp.context = ctx
            bp.save(update_fields=["context"])
            print(f"Authentik bootstrap blueprint now names {target!r} (#2644)")
except Exception as exc:
    print(f"WARN: could not point the bootstrap blueprint at {target!r} (non-fatal; the rename is unaffected): {exc}")
' 2>&1); then
        _rename_rc=0
    else
        _rename_rc=$?
    fi
    # #1148 review, finding 6: this used to read
    #   … | sed 's/^/  /' || echo "  … (non-fatal) …"
    # A pipeline's status is its LAST command, so the `||` watched `sed`, not
    # the rename — the message could never fire and a failed rename passed in
    # silence.
    #
    # The first fix reached for `${PIPESTATUS[0]}`. That is a BASH array, and
    # this file is `#!/bin/sh` run by BusyBox ash inside alpine:3.24 (see
    # core/Authentik/init/Dockerfile — no bash in the image). It is not a wrong
    # value there, it is a SYNTAX ERROR: the shell aborts at that line, and
    # everything below it — including the three apply_policy_bindings calls —
    # never runs. So: capture, check the status of the command itself, then
    # print. `tests/unit/consistency/test_1148_admin_username.py` now parses
    # every `#!/bin/sh` script with dash.
    printf '%s\n' "$_rename_out" | sed 's/^/  /'
    if [ "$_rename_rc" -ne 0 ]; then
        echo "  Superuser rename reported an issue (non-fatal) — the box keeps its current name."
    fi
    return 0
}

# #1350/#1370 — re-apply blueprint instances that failed, one at a time, and
# leave a record. A mass apply after an upgrade puts a variable set of module
# blueprints into `error` (12 of them on 0.79 across ga.15 -> 2026.09; 13 on
# 0.91, different names): each creates a forward-auth provider, every provider
# change fires outpost_send_update on the ONE embedded-outpost row, and dozens
# at once deadlock in postgres. Nothing retries them. Applied serially they all
# went green with zero deadlocks — so that is what this does.
reconcile_blueprints() {
    if ! ensure_init_tools; then
        echo "Skipping blueprint reconcile — init tools unavailable (see ERROR above)."
        return 1
    fi
    if docker cp /init/reconcile-blueprints.py authentik-worker:/tmp/reconcile_blueprints.py; then
        docker exec authentik-worker python /tmp/reconcile_blueprints.py \
            || echo "Blueprint reconcile exited non-zero (non-fatal) — see the lines above."
    else
        echo "Failed to copy the blueprint reconcile script to authentik-worker."
    fi
}

# #2679 — the fast path (version, profiles, domain unchanged) never reached the
# reconcile above, so a module blueprint that deadlocked in an earlier apply
# (#1350) stayed in `error` until the next profile or domain change. One cheap
# read (`reconcile-blueprints.py --check`: exit 0 healthy, 3 something in
# error); only a 3 pays for the serial, idempotent reconcile. Anything else —
# Authentik down, the copy failing — is one log line and the fast path goes on:
# this must never fail init.
reconcile_errored_blueprints() {
    if ! ensure_init_tools; then
        echo "Blueprint health check skipped (#2679) — init tools unavailable."
        return 0
    fi
    if ! docker cp /init/reconcile-blueprints.py authentik-worker:/tmp/reconcile_blueprints.py; then
        echo "Blueprint health unreadable (#2679) — could not copy the reconcile script; fast path continues."
        return 0
    fi
    _bp_rc=0
    docker exec authentik-worker python /tmp/reconcile_blueprints.py --check || _bp_rc=$?
    if [ "$_bp_rc" -eq 3 ]; then
        echo "A blueprint instance is in error — running the serial reconcile (#2679)."
        reconcile_blueprints || true
    elif [ "$_bp_rc" -ne 0 ]; then
        echo "Blueprint health unreadable (#2679, exit $_bp_rc) — Authentik unreachable? Fast path continues."
    fi
    return 0
}

# #2501 — Authentik's own system blueprint "System - OAuth2 Provider - Scopes"
# finds its scope mappings by `managed` key. A built-in row whose key went NULL
# (prod: the OpenID 'email' mapping) makes it try to create a duplicate name, so
# the whole system blueprint stays in `error`. Data fix, not a blueprint edit:
# repair-managed-scope-mappings.py puts the key back only where it is NULL and
# no other row holds it, and logs what it changed. Runs before the reconcile,
# which then re-applies the errored system instance. Never fatal.
repair_managed_scope_mappings() {
    if ! ensure_init_tools; then
        echo "Skipping scope-mapping repair — init tools unavailable (see ERROR above)."
        return 1
    fi
    if docker cp /init/repair-managed-scope-mappings.py authentik-worker:/tmp/repair_managed_scope_mappings.py; then
        docker exec authentik-worker python /tmp/repair_managed_scope_mappings.py \
            || echo "WARN(#2501): scope-mapping repair exited non-zero (non-fatal) — see the lines above."
    else
        echo "WARN(#2501): could not copy the scope-mapping repair script to authentik-worker."
    fi
    return 0
}

# #2522 — the Google and Entra SSO blueprints share their source names
# (10-property-mappings … 13-source). Deployed under those bare names into the
# ONE /blueprints directory, Entra overwrote Google on a box with both enabled
# (prod: the "Google - Flows" instance applied Entra content). Each provider's
# files now land as `<provider>-<file>`, the #29/#30 naming of the OIDC
# blueprints, so both sets coexist. 14-flow-integration is never copied: the
# combined z-15-sso-flow-integration.yaml below replaces it.
copy_sso_blueprints() {
    _sso_p="$1"
    _sso_src="${SOURCE_BLUEPRINTS:-/init/blueprints}/$_sso_p"
    _sso_dst="${TARGET_BLUEPRINTS:-/blueprints}"
    # #2927: the allowed Google domains land inside a quoted expression string: keep
    # only characters a domain list can hold. Unset and set-but-empty are the
    # same: an empty list, and the policy then admits nobody (#3512: no vendor
    # domain as a fallback). Only envsubst sees the cleaned value, so the
    # SSO fingerprint (sso_state_hash) keeps reading the raw one.
    _sso_gd=$(printf '%s' "${GOOGLE_OAUTH_DOMAIN:-}" | tr -cd 'A-Za-z0-9.,@ -')
    # The allowed Microsoft domains land inside quoted expression strings too
    # (the domain restriction and the link-by-e-mail check): the same cleaning.
    _sso_ed=$(printf '%s' "${ENTRA_OAUTH_DOMAIN:-}" | tr -cd 'A-Za-z0-9.,@ -')
    for _sso_file in "$_sso_src"/*.yaml; do
        [ -f "$_sso_file" ] || continue
        _sso_base=$(basename "$_sso_file")
        [ "$_sso_base" = "14-flow-integration.yaml" ] && continue
        echo "Processing $_sso_file -> $_sso_p-$_sso_base"
        GOOGLE_OAUTH_DOMAIN="$_sso_gd" ENTRA_OAUTH_DOMAIN="$_sso_ed" envsubst < "$_sso_file" > "$_sso_dst/$_sso_p-$_sso_base"
    done
    return 0
}

# #2522 — Authentik tracks a blueprint instance by its file path and its name is
# UNIQUE: after the rename above, discovery would try to create "Google - Flows"
# for google-12-flows.yaml while the old instance of that name still sits at
# 12-flows.yaml. migrate-sso-blueprint-instances.py repoints each old instance
# whose name belongs to a deployed provider onto that provider's new path
# (identity, context and history kept) and logs what it moved. Call it AFTER the
# full path removed the old files and BEFORE copy_sso_blueprints writes the new
# ones. $1 = the providers deployed this run. Never fatal.
migrate_sso_blueprint_instances() {
    [ -n "$(echo "${1:-}" | tr -d ' ')" ] || return 0
    if ! ensure_init_tools; then
        echo "WARN(#2522): init tools unavailable — SSO blueprint instances not migrated; Authentik's hourly cleanup retires the old ones."
        return 0
    fi
    if docker cp /init/migrate-sso-blueprint-instances.py authentik-worker:/tmp/migrate_sso_blueprint_instances.py; then
        docker exec -e RZFZ_SSO_PROVIDERS="$1" authentik-worker python /tmp/migrate_sso_blueprint_instances.py \
            || echo "WARN(#2522): SSO blueprint instance migration exited non-zero (non-fatal) — see the lines above."
    else
        echo "WARN(#2522): could not copy the SSO blueprint instance migration to authentik-worker."
    fi
    return 0
}

# #2816 — give the files this run wrote to uid 1000 WITHOUT touching the others.
# Authentik's blueprint watcher re-applies a blueprint on any filesystem event
# for it, ATTRIB (chown/chmod/touch) included and whether or not the content
# changed, so a recursive chown over /blueprints re-applied every blueprint.
chown_new_blueprints() {
    find "${TARGET_BLUEPRINTS:-/blueprints}" -not -user 1000 -exec chown 1000:1000 {} + 2>/dev/null || true
}

# #2794 — delete the BlueprintInstance of a blueprint FILE this run removed.
# Removing the file alone leaves the instance in `error` (file missing) until
# Authentik's hourly cleanup. $1 = file name relative to /blueprints. Never fatal.
drop_blueprint_instance_path() {
    ensure_init_tools >/dev/null 2>&1 || return 0
    if docker cp /init/drop-blueprint-instance.py authentik-worker:/tmp/drop_blueprint_instance.py; then
        docker exec authentik-worker python /tmp/drop_blueprint_instance.py "$1" \
            || echo "WARN(#2794): dropping the blueprint instance for $1 exited non-zero (non-fatal)."
    else
        echo "WARN(#2794): could not copy the blueprint-instance drop script to authentik-worker."
    fi
    return 0
}

# #2794 — a provider that is switched off. Its deployed files go (and their
# instances are dropped). If it was on before (marker, or its source file is
# still deployed), an `state: absent` blueprint removes the source object so
# /source/oauth/login/<slug>/ stops redirecting; the absent file is retired on
# the following run. $1 = slug, $2 = display name, $3 = "true" when it was on.
retire_sso_provider() {
    _rp_slug="$1"; _rp_name="$2"; _rp_was="$3"
    _rp_dst="${TARGET_BLUEPRINTS:-/blueprints}"
    _rp_absent="$_rp_slug-99-source-absent.yaml"
    for _rp_f in "$_rp_dst/$_rp_slug"-*.yaml; do
        [ -f "$_rp_f" ] || continue
        _rp_b=$(basename "$_rp_f")
        [ "$_rp_b" = "$_rp_absent" ] && continue
        rm -f "$_rp_f"
        drop_blueprint_instance_path "$_rp_b"
    done
    if [ "$_rp_was" = "true" ]; then
        {
            echo "version: 1"
            echo "metadata:"
            echo "  name: \"$_rp_name - SSO Source (removed)\""
            echo "  labels:"
            echo "    blueprints.goauthentik.io/instantiate: \"true\""
            echo "entries:"
            echo "  - model: authentik_sources_oauth.oauthsource"
            echo "    state: absent"
            echo "    identifiers:"
            echo "      slug: $_rp_slug"
        } > "$_rp_dst/$_rp_absent"
    elif [ -f "$_rp_dst/$_rp_absent" ]; then
        rm -f "$_rp_dst/$_rp_absent"
        drop_blueprint_instance_path "$_rp_absent"
    fi
    return 0
}

# #2794 — the Google/Entra SSO source blueprints and the combined flow
# integration. Extracted from the full path so the fast path can run the SAME
# code when the SSO inputs change (a Settings-portal toggle used to only restart
# Authentik, which applies what is already in /blueprints — never the Entra
# files). Idempotent: it re-renders the files from the current environment.
deploy_sso_blueprints() {
    _sso_dst="${TARGET_BLUEPRINTS:-/blueprints}"
# Which OAuth login sources are enabled. The identification-stage source list
# is a SET (Authentik blueprint `attrs` overwrites, it does NOT append), so the
# per-provider 14-flow-integration.yaml files clobbered each other (enabling
# Entra dropped Google off the login page). We therefore copy every per-provider
# blueprint EXCEPT its 14-flow-integration.yaml, and generate ONE combined
# z-15-sso-flow-integration.yaml below that lists all enabled sources together.
GOOGLE_SSO_ON=false
ENTRA_SSO_ON=false

if [ "$DEPLOYMENT_SCENARIO" = "google" ] || [ "${ENABLE_GOOGLE_OAUTH:-false}" = "true" ]; then
    GOOGLE_SSO_ON=true
fi

if [ "${ENABLE_ENTRA_OAUTH:-false}" = "true" ]; then
    if [ -z "${ENTRA_TENANT_ID:-}" ]; then
        echo "WARNING: ENABLE_ENTRA_OAUTH=true but ENTRA_TENANT_ID is empty — skipping Entra SSO blueprints"
    elif [ -z "${ENTRA_CLIENT_ID:-}" ] || [ -z "${ENTRA_CLIENT_SECRET:-}" ]; then
        echo "WARNING: ENABLE_ENTRA_OAUTH=true but ENTRA_CLIENT_ID or ENTRA_CLIENT_SECRET is empty — skipping Entra SSO blueprints"
    else
        ENTRA_SSO_ON=true
    fi
else
    echo "ENABLE_ENTRA_OAUTH not set or false — skipping Entra SSO blueprints."
fi

# #2522: the per-provider files are deployed as google-<file> / entra-<file>
# (they used to share bare names and Entra overwrote Google). Move the existing
# instances onto the new names FIRST — before the files appear — so discovery
# finds them by path instead of colliding on their unique name.
SSO_PROVIDERS=""
[ "$GOOGLE_SSO_ON" = "true" ] && SSO_PROVIDERS="$SSO_PROVIDERS google"
[ "$ENTRA_SSO_ON" = "true" ] && SSO_PROVIDERS="$SSO_PROVIDERS entra"
migrate_sso_blueprint_instances "$SSO_PROVIDERS"

# #2794: a provider switched off loses its deployed files (its instances are
# retired by Authentik's cleanup); the flow integration below unbinds the source.
_sso_prev=" $(cat "${INIT_FLAG_FILE}.sso.providers" 2>/dev/null | tr -d '\n') "
if [ "$GOOGLE_SSO_ON" != "true" ]; then
    _sso_wasp=false
    case "$_sso_prev" in *" google "*) _sso_wasp=true ;; esac
    [ -f "$_sso_dst/google-13-source.yaml" ] && _sso_wasp=true
    retire_sso_provider google Google "$_sso_wasp"
fi
if [ "$ENTRA_SSO_ON" != "true" ]; then
    _sso_wasp=false
    case "$_sso_prev" in *" entra "*) _sso_wasp=true ;; esac
    [ -f "$_sso_dst/entra-13-source.yaml" ] && _sso_wasp=true
    retire_sso_provider entra Entra "$_sso_wasp"
fi

if [ "$GOOGLE_SSO_ON" = "true" ]; then
    echo "Copying Google SSO blueprints..."
    copy_sso_blueprints google
fi
if [ "$ENTRA_SSO_ON" = "true" ]; then
    echo "Copying Entra SSO blueprints..."
    copy_sso_blueprints entra
    echo "Entra SSO blueprints deployed."
fi

# --- Combined SSO flow-integration: ONE blueprint that sets the identification
#     stage's source list to ALL enabled providers at once. Sorts after the
#     per-provider files (z-15 > 13-source) so it always wins. Legacy per-provider
#     z-14-*-flow-integration.yaml files are removed so they can't re-clobber.
rm -f "$_sso_dst/z-14-google-flow-integration.yaml" "$_sso_dst/z-14-entra-flow-integration.yaml"
SSO_FI="$_sso_dst/z-15-sso-flow-integration.yaml"
if [ "$GOOGLE_SSO_ON" = "true" ] || [ "$ENTRA_SSO_ON" = "true" ]; then
    echo "Generating combined SSO flow-integration (google=$GOOGLE_SSO_ON entra=$ENTRA_SSO_ON)..."
    {
        echo "version: 1"
        echo "metadata:"
        echo "  name: \"SSO - Flow Integration (combined)\""
        echo "  labels:"
        echo "    blueprints.goauthentik.io/instantiate: \"true\""
        echo "entries:"
        echo "  - model: authentik_stages_identification.identificationstage"
        echo "    identifiers:"
        echo "      name: default-authentication-identification"
        echo "    attrs:"
        echo "      sources:"
        [ "$GOOGLE_SSO_ON" = "true" ] && echo "        - !Find [authentik_sources_oauth.oauthsource, [slug, google]]"
        [ "$ENTRA_SSO_ON" = "true" ] && echo "        - !Find [authentik_sources_oauth.oauthsource, [slug, entra]]"
    } > "$SSO_FI"
elif [ -n "$(cat "${INIT_FLAG_FILE}.sso.providers" 2>/dev/null | tr -d ' ')" ]; then
    # #2794: SSO was on and is now off. Deleting the file would leave the
    # identification stage's source list as the last apply wrote it (the button
    # stays), so the file stays, with an EMPTY source list, until the next run
    # records the off state.
    echo "SSO disabled — emptying the identification stage's source list (#2794)."
    {
        echo "version: 1"
        echo "metadata:"
        echo "  name: \"SSO - Flow Integration (combined)\""
        echo "  labels:"
        echo "    blueprints.goauthentik.io/instantiate: \"true\""
        echo "entries:"
        echo "  - model: authentik_stages_identification.identificationstage"
        echo "    identifiers:"
        echo "      name: default-authentication-identification"
        echo "    attrs:"
        # Authentik rejects an empty source list on a stage with no user
        # fields ("When no user fields are selected, at least one source must be
        # selected") and keeps the old binding. These are the default stage's.
        echo "      user_fields:"
        echo "        - email"
        echo "        - username"
        echo "      sources: []"
    } > "$SSO_FI"
else
    # No SSO sources enabled — make sure no stale combined file lingers, and
    # no instance of it is left in `error` (file missing).
    if [ -f "$SSO_FI" ]; then
        rm -f "$SSO_FI"
        drop_blueprint_instance_path "z-15-sso-flow-integration.yaml"
    fi
fi
    SSO_PROVIDERS_NOW="$SSO_PROVIDERS"
    return 0
}

# #2794 — a fingerprint of every input of the SSO step. Stored next to
# .authentik_initialized.domain; a difference on the fast path runs
# deploy_sso_blueprints. The secrets enter only as part of the digest.
sso_state_hash() {
    # The rule line changes the hash when what an unchanged .env renders to
    # changes: #3512 (an unset GOOGLE_OAUTH_DOMAIN renders empty, no fallback).
    printf '%s\n' \
        "rule=3512-unset-google-domain-renders-empty" \
        "scenario=${DEPLOYMENT_SCENARIO:-}" \
        "google=${ENABLE_GOOGLE_OAUTH:-}:${GOOGLE_CLIENT_ID:-}:${GOOGLE_CLIENT_SECRET:-}:${GOOGLE_OAUTH_DOMAIN:-}" \
        "entra=${ENABLE_ENTRA_OAUTH:-}:${ENTRA_TENANT_ID:-}:${ENTRA_CLIENT_ID:-}:${ENTRA_CLIENT_SECRET:-}:${ENTRA_OAUTH_DOMAIN:-}" \
        | sha256sum | cut -d' ' -f1
}

# #2794 — remember what the SSO step last delivered (hash + the providers).
record_sso_state() {
    sso_state_hash > "${INIT_FLAG_FILE}.sso"
    echo "${SSO_PROVIDERS_NOW:-}" > "${INIT_FLAG_FILE}.sso.providers"
}

# #2816 — the serial retry restricted to the SSO blueprint files. The unscoped
# reconcile re-applies every instance in error, whatever it is.
reconcile_sso_blueprints() {
    if ! ensure_init_tools; then
        echo "Skipping SSO blueprint reconcile — init tools unavailable."
        return 1
    fi
    if docker cp /init/reconcile-blueprints.py authentik-worker:/tmp/reconcile_blueprints.py; then
        docker exec -e RZFZ_BLUEPRINT_PATH_PREFIXES="google-,entra-,z-15-sso" authentik-worker \
            python /tmp/reconcile_blueprints.py \
            || echo "SSO blueprint reconcile exited non-zero (non-fatal) — see the lines above."
    else
        echo "Failed to copy the blueprint reconcile script to authentik-worker."
    fi
}

# #2816 — does the embedded outpost hold every ProxyProvider? A mismatch means
# the module apps answer 404 at Caddy's forward_auth. Reports, never fails init.
check_outpost_providers() {
    ensure_init_tools >/dev/null 2>&1 || return 0
    docker exec authentik-worker python -c '
import os, sys
sys.path.append("/")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
import django
django.setup()
from authentik.outposts.models import Outpost
from authentik.providers.proxy.models import ProxyProvider
o = Outpost.objects.filter(name="authentik Embedded Outpost").first()
total = ProxyProvider.objects.count()
have = o.providers.count() if o else -1
if have == total:
    print("Outpost check: all %d proxy providers attached." % total)
else:
    print("WARN: the embedded outpost holds %d of %d proxy providers - module apps would answer 404; run: rzfz post-install --refresh" % (have, total))
' || echo "WARN: the outpost provider check could not run."
    return 0
}

# #2794 — the fast path's SSO step: run only when the fingerprint changed (a
# missing marker counts as changed: a box that enabled SSO before this existed
# never got its sources). Waits for the worker to apply, then retries whatever
# errored (the flow integration can win the race against the source files).
sso_fast_path_step() {
    _sso_now=$(sso_state_hash)
    _sso_was=$(cat "${INIT_FLAG_FILE}.sso" 2>/dev/null || echo "")
    if [ "$_sso_now" = "$_sso_was" ]; then
        return 0
    fi
    echo "SSO configuration changed — delivering the SSO source blueprints (#2794)..."
    ensure_init_tools || echo "WARN(#2794): init tools unavailable — SSO blueprint instances may not migrate."
    deploy_sso_blueprints
    # #2816: only files not yet owned by 1000. `chown -R` touches EVERY file; an
    # owner-unchanged chown still fires inotify ATTRIB, which Authentik's
    # blueprint watcher takes for a modification and re-applies the blueprint
    # without a content check (04-outposts then reset the outpost's providers).
    chown_new_blueprints
    record_sso_state
    sleep "${SSO_APPLY_WAIT:-20}"   # let the worker pick the new files up
    # #2816: retry ONLY the SSO instances (never re-apply a non-SSO blueprint),
    # then run the binding pass again AFTER the worker has settled: anything
    # that re-applied the outpost blueprint in the meantime is converged back
    # to the full provider set, and the final check says so.
    reconcile_sso_blueprints || true
    rename_bootstrap_admin
    apply_policy_bindings
    check_outpost_providers || true
    return 0
}

# #2501 — 18-synapse-oidc.yaml is a base blueprint, so every path copied it,
# matrix or not. Without the matrix profile there are no Synapse credentials and
# the provider entry fails validation ("client_id: This field may not be
# blank"), leaving "Module - Synapse Matrix OIDC" in `error` for good. Keep the
# rendered file only when the matrix profile is active AND both credentials are
# set; otherwise remove it (Authentik's clear_failed_blueprints drops the
# instance whose file is gone). Call AFTER a path has copied the base set.
gate_synapse_blueprint() {
    _syn_target="${TARGET_BLUEPRINTS:-/blueprints}/18-synapse-oidc.yaml"
    if ! echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "matrix"; then
        if [ -f "$_syn_target" ]; then
            rm -f "$_syn_target"
            echo "matrix profile off — 18-synapse-oidc.yaml not deployed (#2501)."
        fi
        return 0
    fi
    if [ -z "${MATRIX_CLIENT_ID:-}" ] || [ -z "${SYNAPSE_CLIENT_SECRET:-}" ]; then
        rm -f "$_syn_target"
        echo "WARNING(#2501): matrix profile on but MATRIX_CLIENT_ID or SYNAPSE_CLIENT_SECRET empty — 18-synapse-oidc.yaml not deployed; Matrix SSO has no provider until both are set."
        return 0
    fi
    return 0
}

# #2536 item 15 — gate_synapse_blueprint removes the FILE; the INSTANCE was
# left to Authentik's hourly clear_failed_blueprints and sat in `error` until
# then. Delete it whenever the file is not deployed. Runs next to
# repair_managed_scope_mappings, i.e. on every path. Never fatal.
drop_gated_synapse_instance() {
    [ -f "${TARGET_BLUEPRINTS:-/blueprints}/18-synapse-oidc.yaml" ] && return 0
    if ! ensure_init_tools; then
        echo "WARN(#2536): init tools unavailable — the gated-out Synapse blueprint instance is left to Authentik's hourly cleanup."
        return 0
    fi
    if docker cp /init/drop-blueprint-instance.py authentik-worker:/tmp/drop_blueprint_instance.py; then
        docker exec authentik-worker python /tmp/drop_blueprint_instance.py 18-synapse-oidc.yaml \
            || echo "WARN(#2536): dropping the gated-out Synapse blueprint instance exited non-zero (non-fatal)."
    else
        echo "WARN(#2536): could not copy the blueprint-instance drop script to authentik-worker."
    fi
    return 0
}

# #2476: 38-openbao.yaml only with the openbao profile and its three credentials
# (the proxy secret, the OIDC client id and secret) — a blank client_id
# otherwise, the 18-synapse-oidc lesson.
gate_openbao_blueprint() {
    _bao_target="${TARGET_BLUEPRINTS:-/blueprints}/38-openbao.yaml"
    if ! echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "openbao"; then
        if [ -f "$_bao_target" ]; then
            rm -f "$_bao_target"
            echo "openbao profile off — 38-openbao.yaml not deployed."
        fi
        return 0
    fi
    if [ -z "${OPENBAO_CLIENT_SECRET:-}" ] || [ -z "${OPENBAO_OIDC_CLIENT_ID:-}" ] || [ -z "${OPENBAO_OIDC_CLIENT_SECRET:-}" ]; then
        rm -f "$_bao_target"
        echo "WARNING: openbao profile on but OPENBAO_CLIENT_SECRET, OPENBAO_OIDC_CLIENT_ID or OPENBAO_OIDC_CLIENT_SECRET empty — 38-openbao.yaml not deployed; bao.<domain> has no Authentik provider until all three are set (rzfz upgrade generates them)."
        return 0
    fi
    return 0
}

# #2816: set when the module-binding / outpost reconcile was skipped or failed.
# The run then exits NON-ZERO (see the checks before each baseline write) and records no baseline, so the
# portal apply fails and the next run does the work again.
RECONCILE_FAILED=0

apply_policy_bindings() {
    # #147: guarantee docker is present in EVERY path that reconciles bindings
    # (the always-run fast path previously called this with no apk → 'docker:
    # not found'). Fail loud + skip rather than crash the whole init.
    if ! ensure_init_tools; then
        echo "Skipping outpost-binding reconcile — init tools unavailable (see ERROR above)."
        RECONCILE_FAILED=1
        return 1
    fi
    if docker cp /init/apply-policy-bindings.py authentik-worker:/tmp/apply_policy_bindings.py; then
        # #2507: the two native-OIDC switches tell the script whether to WAIT
        # for the openwebui-oidc / gitea-oidc applications (switch on) or only
        # bind them if present (switch off — no blueprint, no app).
        # #2551: RAZZFAZZ_REQUIRE_ADMIN_MFA is passed on EVERY exec. The worker's
        # own environment is whatever it was created with, so without -e a .env
        # change reaches the script only after the worker is recreated. Empty
        # or unset means true, as the script reads it.
        # A1 D10: RAZZFAZZ_ACCESS_GROUP likewise, ALWAYS set (empty = lift the
        # restriction); the script leaves the restriction alone only when the
        # variable is absent, i.e. on a hand-run without -e.
        docker exec -e COMPOSE_PROFILES="${COMPOSE_PROFILES:-}" \
            -e RAZZFAZZ_ADMIN_USERNAME="${RAZZFAZZ_ADMIN_USERNAME:-}" \
            -e RAZZFAZZ_REQUIRE_ADMIN_MFA="${RAZZFAZZ_REQUIRE_ADMIN_MFA:-}" \
            -e RAZZFAZZ_ACCESS_GROUP="${RAZZFAZZ_ACCESS_GROUP:-}" \
            -e ENABLE_OPENWEBUI_OIDC="${ENABLE_OPENWEBUI_OIDC:-false}" \
            -e ENABLE_GITEA_AUTHENTIK_OIDC="${ENABLE_GITEA_AUTHENTIK_OIDC:-false}" \
            authentik-worker python /tmp/apply_policy_bindings.py || { echo "Failed to execute binding script."; RECONCILE_FAILED=1; }
    else
        echo "Failed to copy binding script to authentik-worker."
        RECONCILE_FAILED=1
    fi
}

# #2507 — the native-OIDC provider blueprints for Open WebUI and Gitea, on EVERY
# path (full, profiles-changed, and the version-gate fast path).
#
# They used to be templated only on the FULL path. On an upgraded box that path
# does not run (INIT_VERSION/profiles/domain unchanged), so a box that got its
# client secret generated by `rzfz upgrade` — or flipped the switch in the
# Settings UI — never received the provider: prod needed the documented "delete
# /data/media/.authentik_initialized" full re-run on 2026-09-26. Now every path
# renders them, and only a CHANGED rendering is written (the worker's file
# watcher re-applies whatever is touched, so an unchanged file stays untouched).
#
# Sets OIDC_EXPECTED_APPS (slugs whose provider must exist after this run) and
# OIDC_CHANGED_APPS (slugs whose file was written or removed now) for
# wait_for_oidc_blueprints. The target keeps the #29/#30 `<module>-<file>` name.
deploy_oidc_blueprints() {
    _oidc_src="${SOURCE_BLUEPRINTS:-/init/blueprints}"
    _oidc_dst="${TARGET_BLUEPRINTS:-/blueprints}"
    OIDC_EXPECTED_APPS=""
    OIDC_CHANGED_APPS=""
    for _oidc_mod in openwebui-oidc gitea-oidc; do
        case "$_oidc_mod" in
            openwebui-oidc)
                _oidc_label="Open WebUI"; _oidc_switch_name="ENABLE_OPENWEBUI_OIDC"; _oidc_profile="chat"
                _oidc_switch="${ENABLE_OPENWEBUI_OIDC:-false}"
                _oidc_id="${OPENWEBUI_OIDC_CLIENT_ID:-}"; _oidc_secret="${OPENWEBUI_OIDC_CLIENT_SECRET:-}"
                _oidc_keys="OPENWEBUI_OIDC_CLIENT_ID or SECRET" ;;
            *)
                _oidc_label="Gitea"; _oidc_switch_name="ENABLE_GITEA_AUTHENTIK_OIDC"; _oidc_profile="gitea"
                _oidc_switch="${ENABLE_GITEA_AUTHENTIK_OIDC:-false}"
                _oidc_id="${GITEA_OIDC_CLIENT_ID:-}"; _oidc_secret="${GITEA_OIDC_CLIENT_SECRET:-}"
                _oidc_keys="GITEA_OIDC_CLIENT_ID or SECRET" ;;
        esac
        # #2943 — the switch alone is not enough: ENABLE_GITEA_AUTHENTIK_OIDC defaults
        # to true, so a box WITHOUT the gitea profile deployed a blueprint whose
        # application never appears and waited 180 s for it. The module's profile
        # must be enabled too; enabling it later re-runs init with the new
        # COMPOSE_PROFILES and deploys the blueprint then.
        _oidc_skip=""
        if [ "$_oidc_switch" != "true" ]; then
            _oidc_skip="$_oidc_switch_name not set or false"
        elif ! echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "$_oidc_profile"; then
            _oidc_skip="$_oidc_profile profile not enabled"
        fi
        if [ -n "$_oidc_skip" ]; then
            echo "$_oidc_skip — skipping $_oidc_label OIDC blueprint."
            # Parity with the full path, which wipes every *.yaml first: a
            # switched-off provider's file must not linger on the fast path.
            for _oidc_old in "$_oidc_dst/$_oidc_mod"-*.yaml; do
                [ -f "$_oidc_old" ] || continue
                rm -f "$_oidc_old"
                OIDC_CHANGED_APPS="$OIDC_CHANGED_APPS $_oidc_mod"
            done
            continue
        fi
        if [ -z "$_oidc_id" ] || [ -z "$_oidc_secret" ]; then
            echo "WARNING: $_oidc_switch_name=true but $_oidc_keys empty — skipping $_oidc_label OIDC blueprint"
            continue
        fi
        if ! command -v envsubst >/dev/null 2>&1; then
            echo "WARNING(#2507): envsubst unavailable — cannot template the $_oidc_label OIDC blueprint (see ERROR(#147) above)."
            continue
        fi
        echo "Deploying $_oidc_label OIDC provider blueprint..."
        _oidc_wrote=false
        for _oidc_file in "$_oidc_src/$_oidc_mod"/*.yaml; do
            [ -f "$_oidc_file" ] || continue
            _oidc_base=$(basename "$_oidc_file")
            _oidc_target="$_oidc_dst/$_oidc_mod-$_oidc_base"
            # Rendered next to the target under a name discovery does not glob
            # (*.yaml), then moved into place only when it differs.
            _oidc_tmp="$_oidc_dst/.$_oidc_mod-$_oidc_base.rzfz-tmp"
            if ! envsubst < "$_oidc_file" > "$_oidc_tmp"; then
                echo "WARNING(#2507): templating $_oidc_file failed — $_oidc_target left as it was."
                rm -f "$_oidc_tmp"
                continue
            fi
            if [ -f "$_oidc_target" ] && cmp -s "$_oidc_tmp" "$_oidc_target"; then
                rm -f "$_oidc_tmp"
            else
                mv -f "$_oidc_tmp" "$_oidc_target"
                chown 1000:1000 "$_oidc_target" 2>/dev/null || true
                _oidc_wrote=true
            fi
        done
        OIDC_EXPECTED_APPS="$OIDC_EXPECTED_APPS $_oidc_mod"
        if [ "$_oidc_wrote" = "true" ]; then
            OIDC_CHANGED_APPS="$OIDC_CHANGED_APPS $_oidc_mod"
            echo "$_oidc_label OIDC provider blueprint deployed."
        else
            echo "$_oidc_label OIDC provider blueprint unchanged."
        fi
    done
    return 0
}

# #2507 — Authentik applies a written blueprint ASYNCHRONOUSLY: on prod both
# OIDC providers appeared seconds AFTER init had ended, and a discovery check at
# init's end still said 404. So the policy-binding pass (and everything after
# init) must not rely on the file — wait for the applied instance + application
# (core/Authentik/wait-oidc-blueprints.py). Loud, never fatal: returns 1 and
# prints ERROR(#2507) when a provider did not appear in time.
wait_for_oidc_blueprints() {
    [ -n "$(echo "${OIDC_EXPECTED_APPS:-}" | tr -d ' ')" ] || return 0
    if ! ensure_init_tools; then
        echo "ERROR(#2507): cannot verify the OIDC provider blueprints — init tools unavailable."
        return 1
    fi
    echo "Waiting for the OIDC provider blueprints to apply:${OIDC_EXPECTED_APPS} (#2507)..."
    if ! docker cp /init/wait-oidc-blueprints.py authentik-worker:/tmp/wait_oidc_blueprints.py; then
        echo "ERROR(#2507): could not copy wait-oidc-blueprints.py to authentik-worker — OIDC providers NOT verified."
        return 1
    fi
    if docker exec -e RZFZ_OIDC_APPS="${OIDC_EXPECTED_APPS}" \
            -e RZFZ_OIDC_CHANGED="${OIDC_CHANGED_APPS:-}" \
            authentik-worker python /tmp/wait_oidc_blueprints.py; then
        return 0
    fi
    echo "ERROR(#2507): an OIDC provider blueprint did not apply (see the line above) — native SSO login for it stays broken until it does."
    return 1
}

# #2755: the tenant avatar setting, as a function so BOTH paths run it. The tenant row
# (authentik_tenants_tenant.avatars) decides, not the container's env, and a box that
# upgrades with an unchanged INIT_VERSION takes the fast path, which used to skip it:
# measured on an upgraded box, the row still read 'gravatar,initials'. Idempotent (it
# writes only when the row differs) and never fatal.
apply_tenant_avatars() {
    # #2755: unconditional: the tenant row (not the env) decides, and a box without Google/Entra
    # SSO would otherwise keep the default 'gravatar,initials'.
    echo "Applying system settings (Avatars) directly to DB..."
    cat <<EOF > /tmp/apply_tenant_settings.py
import os
import sys
import django

sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.tenants.models import Tenant

try:
    t = Tenant.objects.get(schema_name="public")
    # #2755: follow the container's AUTHENTIK_AVATARS (initials by default, never gravatar)
    want = os.environ.get("AUTHENTIK_AVATARS") or "initials"
    if t.avatars != want:
        print(f"Updating avatars from '{t.avatars}' to '{want}'")
        t.avatars = want
        t.save()
        print("Avatars updated successfully.")
    else:
        print("Avatars already set correctly.")
except Exception as e:
    print(f"Error updating tenant: {e}")
EOF

    # Copy script to worker and execute
    # Note: We rely on the docker socket being mounted in creating this container
    echo "Executing DB update script in authentik-worker..."
    if docker cp /tmp/apply_tenant_settings.py authentik-worker:/tmp/apply_tenant_settings.py; then
        docker exec authentik-worker python /tmp/apply_tenant_settings.py || echo "Failed to execute python script."
    else
        echo "Failed to copy script to authentik-worker (Docker socket might be missing)."
    fi
}

# #183: last-init-domain tracking, alongside the version/profiles flags. A
# MAIN_DOMAIN change moves every ProxyProvider's `external_host` and every
# native-OIDC provider's `redirect_uris` (templated with ${OPENWEBUI_DOMAIN} /
# ${GITEA_DOMAIN} / etc. — all derived from MAIN_DOMAIN — straight into
# 03-providers-apps.yaml and the per-module/OIDC blueprints), so it needs the
# SAME full blueprint `envsubst` re-template as a version bump. Before this
# fix, neither fast-path branch below noticed a domain-only change (only
# INIT_VERSION and COMPOSE_PROFILES were compared) — the blueprints kept the
# OLD domain baked in and the embedded outpost 404'd every SSO-gated app at
# the new host (bug #183).
INIT_DOMAIN_FLAG_FILE="${INIT_FLAG_FILE}.domain"

if [ -f "$INIT_FLAG_FILE" ]; then
    STORED_VERSION=$(cat "$INIT_FLAG_FILE" 2>/dev/null || echo "0")
    STORED_PROFILES=$(cat "${INIT_FLAG_FILE}.profiles" 2>/dev/null || echo "")
    STORED_DOMAIN=$(cat "$INIT_DOMAIN_FLAG_FILE" 2>/dev/null || echo "")
    CURRENT_PROFILES="${COMPOSE_PROFILES:-}"
    CURRENT_DOMAIN="${MAIN_DOMAIN:-}"

    # An EMPTY stored domain means the tracking file predates this fix (an
    # existing box upgrading across it) — treat that as "unknown, not a
    # change" so the first run after upgrade doesn't force a surprise full
    # re-template on every already-deployed box. The file gets backfilled
    # below on every branch; a REAL domain change is caught starting on the
    # next run once the baseline is recorded.
    DOMAIN_CHANGED=false
    if [ -n "$STORED_DOMAIN" ] && [ "$STORED_DOMAIN" != "$CURRENT_DOMAIN" ]; then
        DOMAIN_CHANGED=true
    fi

    if [ "$STORED_VERSION" = "$INIT_VERSION" ] && [ "$STORED_PROFILES" = "$CURRENT_PROFILES" ] && [ "$DOMAIN_CHANGED" = "false" ]; then
        echo "=============================================="
        echo "Authentik already initialized (version $INIT_VERSION)."
        echo "Profiles unchanged ($CURRENT_PROFILES)."
        echo "Domain unchanged ($CURRENT_DOMAIN)."
        echo "Skipping blueprint copy + flow setup to preserve user changes."
        echo "To force full re-init, delete: $INIT_FLAG_FILE"
        echo "=============================================="
        # v2026.05-ga.4 hotfix round 17 (operator-reported 2026-05-21,
        # a test box): the version-gate fast-path used to `exit 0`
        # here, which silently skipped apply-policy-bindings.py. Result:
        # ProxyProvider rows that landed via blueprints between init runs
        # never got attached to the embedded outpost → Caddy forward_auth
        # returned 404 instead of 302 → users hit Authentik's catch-all
        # 404 page with the broken-logo "Not Found / Zur Startseite" screen.
        # 14 providers were silently un-bound on the prod box; manual
        # `apply-policy-bindings.py` invocation reattached them.
        # Fix: ALWAYS run policy-bindings reconcile, even on the fast-path.
        # The script is idempotent (binding-already-exists branches no-op),
        # so re-runs are cheap and self-healing.
        # #2507: the native-OIDC providers ride the fast path too — this is
        # the path an UPGRADED box takes, and the one where `rzfz upgrade` just
        # generated the client secrets the full path used to be the only
        # consumer of. Rendered + written only when changed, then WAITED for
        # (async apply) so the binding pass below finds the applications.
        ensure_init_tools || echo "WARN(#147): init tools unavailable — OIDC blueprints cannot be templated."
        deploy_oidc_blueprints
        wait_for_oidc_blueprints || true
        # #2501: cheap and idempotent — a NULL managed key on a built-in scope
        # mapping keeps Authentik's system blueprint in error on any path.
        repair_managed_scope_mappings || true
        drop_gated_synapse_instance
        echo "Reconciling outpost provider bindings (always-run, idempotent)..."
        rename_bootstrap_admin
        apply_policy_bindings
        # #2679: a blueprint left in `error` by an earlier deadlock (#1350) is
        # re-applied serially; a healthy box pays one read.
        reconcile_errored_blueprints
        # #2794: the Google/Entra sources, when their inputs changed.
        sso_fast_path_step
        # #2755: the tenant avatars row, on the path an upgraded box takes.
        apply_tenant_avatars || true
        # #526: always-run too — see copy_media_assets(). A new branding asset
        # otherwise never reaches /data/media/public/ on an upgrade that does not
        # bump INIT_VERSION, and every surface 404s its background.
        copy_media_assets
        # #2816: a run whose reconcile was skipped exits non-zero and records nothing.
        # Every path ends with the outpost verdict (reports, never fails).
        check_outpost_providers || true
        [ "${RECONCILE_FAILED:-0}" = "0" ] || { echo "ERROR: the module-binding / outpost reconcile was skipped or failed (see above) — authentik-init exits non-zero. Re-run: rzfz post-install --refresh"; exit 1; }
        # Backfill the domain-tracking file (covers both the "never existed"
        # upgrade case and the ordinary steady-state re-write).
        echo "$CURRENT_DOMAIN" > "$INIT_DOMAIN_FLAG_FILE"
        exit 0
    elif [ "$STORED_VERSION" = "$INIT_VERSION" ] && [ "$DOMAIN_CHANGED" = "true" ]; then
        # #183: domain changed — this needs the FULL blueprint re-template
        # (every provider's external_host / redirect_uris), not just the
        # limited launch-URL file list the profiles-only fast path re-copies.
        # Falling through (no PROFILES_CHANGED_ONLY, no exit) drops into the
        # full re-init path below, exactly like a version bump.
        echo "MAIN_DOMAIN changed from '$STORED_DOMAIN' to '$CURRENT_DOMAIN'. Forcing full blueprint re-template (#183)..."
    elif [ "$STORED_VERSION" = "$INIT_VERSION" ] && [ "$STORED_PROFILES" != "$CURRENT_PROFILES" ]; then
        echo "Profiles changed from '$STORED_PROFILES' to '$CURRENT_PROFILES'. Re-running app visibility update..."
        PROFILES_CHANGED_ONLY=true
    else
        echo "Init version changed from $STORED_VERSION to $INIT_VERSION. Re-running initialization..."
        # #2403: a start stamp beside the marker. An attached run that dies with
        # the operator's terminal leaves the marker at the OLD version and the
        # providers half-applied — indistinguishable from "never ran". The stamp
        # is removed only when the full apply completes below, so a stale stamp
        # reads as PARTIAL to `rzfz upgrade` and `rzfz status`.
        echo "$INIT_VERSION" > "${INIT_FLAG_FILE}.started"
    fi
fi

# Fast-path: If only profiles changed, re-copy blueprints with updated launch URLs and exit
if [ "${PROFILES_CHANGED_ONLY:-false}" = "true" ]; then
    echo "Installing tools for profile update..."
    ensure_init_tools || echo "WARN(#147): init tools unavailable — blueprint templating may fail."
    
    SOURCE_BLUEPRINTS="/init/blueprints"
    TARGET_BLUEPRINTS="/blueprints"
    ACTIVE_PROFILES="${COMPOSE_PROFILES:-}"
    HIDDEN_URL="blank://blank"

    # Helper: check if a profile is active
    profile_active() { echo "$ACTIVE_PROFILES" | tr ',' '\n' | grep -qx "$1"; }

    # M028 — start portal is core (always-on, no profile gate). Always exported.
    export START_LAUNCH_URL="https://${START_DOMAIN}"

    # Set profile-based launch URLs
    if profile_active "chat"; then
        export CHAT_LAUNCH_URL="https://${OPENWEBUI_DOMAIN}"
    else
        export CHAT_LAUNCH_URL="$HIDDEN_URL"
    fi

    if profile_active "dify" || profile_active "workflow-automation"; then
        export DIFY_LAUNCH_URL="https://${DIFY_DOMAIN}"
    else
        export DIFY_LAUNCH_URL="$HIDDEN_URL"
    fi

    if profile_active "monitor"; then
        export ADMIN_LAUNCH_URL="https://${KOMODO_DOMAIN}"
    else
        export ADMIN_LAUNCH_URL="$HIDDEN_URL"
    fi

    if profile_active "llm" || profile_active "llm-legacy" || profile_active "llm-cpu"; then
        export LLM_LAUNCH_URL="https://${GPUSTACK_DOMAIN}"
    else
        export LLM_LAUNCH_URL="$HIDDEN_URL"
    fi

    if profile_active "gitea"; then
        export GIT_LAUNCH_URL="https://${GITEA_DOMAIN}"
    else
        export GIT_LAUNCH_URL="$HIDDEN_URL"
    fi

    # LightRAG → lightrag profile
    if profile_active "lightrag"; then
        export RAG_LAUNCH_URL="https://${LIGHTRAG_DOMAIN}"
    else
        export RAG_LAUNCH_URL="$HIDDEN_URL"
    fi

    # Cognee → cognee profile
    if profile_active "cognee"; then
        export COGNEE_LAUNCH_URL="https://${COGNEE_DOMAIN}/docs"
    else
        export COGNEE_LAUNCH_URL="$HIDDEN_URL"
    fi

    # Docling → docling profile
    if profile_active "docling"; then
        export DOCLING_LAUNCH_URL="https://docling.${MAIN_DOMAIN}/docs"
    else
        export DOCLING_LAUNCH_URL="$HIDDEN_URL"
    fi

    # Stirling-PDF → stirling-pdf profile
    if profile_active "stirling-pdf"; then
        export STIRLING_LAUNCH_URL="https://pdf.${MAIN_DOMAIN}"
    else
        export STIRLING_LAUNCH_URL="$HIDDEN_URL"
    fi

    # Paperclip → paperclip profile
    if profile_active "paperclip"; then
        export PAPERCLIP_LAUNCH_URL="https://${PAPERCLIP_DOMAIN:-paperclip.${MAIN_DOMAIN}}"
    else
        export PAPERCLIP_LAUNCH_URL="$HIDDEN_URL"
    fi

    # AI Agents → agents profile
    if profile_active "agents"; then
        export AGENTS_LAUNCH_URL="https://${AGENTS_DOMAIN}"
        export MY_AGENTS_LAUNCH_URL="https://${AGENTS_DOMAIN}/dashboard"
    else
        export AGENTS_LAUNCH_URL="$HIDDEN_URL"
        export MY_AGENTS_LAUNCH_URL="$HIDDEN_URL"
    fi

    # rc6.7 #94: blueprint state gating. Setting meta_launch_url=blank://blank
    # leaves the icon visible in the App library — Authentik renders apps
    # regardless of launch URL. The robust hide is `state: absent` on both
    # the application and proxyprovider entries, which DELETES them when
    # the profile is off; the blueprint reapplies them on next enable.
    # Each profile-gated module exposes a *_BLUEPRINT_STATE env var the
    # blueprint substitutes into the entry's `state:` field.
    # Crawl4AI → crawl4ai profile
    if profile_active "crawl4ai"; then
        export CRAWL4AI_LAUNCH_URL="https://crawl4ai.${MAIN_DOMAIN}/playground"
        export CRAWL4AI_BLUEPRINT_STATE="present"
    else
        export CRAWL4AI_LAUNCH_URL="$HIDDEN_URL"
        export CRAWL4AI_BLUEPRINT_STATE="absent"
    fi

    # Observability → observability profile
    if profile_active "observability"; then
        export OBSERVABILITY_LAUNCH_URL="https://${OBSERVABILITY_DOMAIN}"
    else
        export OBSERVABILITY_LAUNCH_URL="$HIDDEN_URL"
    fi

    echo "Updating blueprints for profiles: $ACTIVE_PROFILES"
    echo "  Chat: $CHAT_LAUNCH_URL | Dify: $DIFY_LAUNCH_URL | Admin: $ADMIN_LAUNCH_URL | LLM: $LLM_LAUNCH_URL | Git: $GIT_LAUNCH_URL | RAG: $RAG_LAUNCH_URL"

    # Re-copy blueprints that contain profile-dependent launch URLs.
    # #1176: EVERY base/NN-*.yaml from 10 upwards carries ${…} values and
    # belongs here — 33-llm-manager-ui and 34-hub were missing, so a pure
    # profile change (INIT_VERSION unchanged) never re-templated the LLM
    # Manager and Hub providers. tests/unit/consistency/
    # test_1176_blueprint_recopy_list_complete.py reads this list and refuses
    # the next omission at collection time.
    for bp in 06-oauth-groups-scope.yaml 03-providers-apps.yaml 10-lightrag.yaml 11-cognee.yaml 12-docling.yaml 13-stirling-pdf.yaml 16-paperclip.yaml 17-element-web.yaml 18-synapse-oidc.yaml 19-paperless-ngx.yaml 20-vaultwarden.yaml 21-infisical.yaml 22-onyx.yaml 25-config.yaml 26-agents.yaml 27-observability.yaml 28-my-agents.yaml 29-crawl4ai.yaml 30-start-portal.yaml 31-mcp-manager.yaml 33-llm-manager-ui.yaml 34-hub.yaml 35-openuem.yaml 36-searxng.yaml 37-wazuh.yaml 38-openbao.yaml; do
        rm -f "$TARGET_BLUEPRINTS/$bp" 2>/dev/null || true
        if [ -f "$SOURCE_BLUEPRINTS/base/$bp" ]; then
            envsubst < "$SOURCE_BLUEPRINTS/base/$bp" > "$TARGET_BLUEPRINTS/$bp"
            chown 1000:1000 "$TARGET_BLUEPRINTS/$bp"
            echo "  Updated: $bp"
        fi
    done
    # 2026.11: a module the stack no longer ships leaves no rendered blueprint
    # behind (apply-policy-bindings.py removes what it had created).
    rm -f "$TARGET_BLUEPRINTS/23-openhands.yaml" 2>/dev/null || true
    # #2501: the loop above re-copies 18-synapse-oidc.yaml; keep it only with matrix + credentials.
    gate_synapse_blueprint
    gate_openbao_blueprint

    # #2507: the native-OIDC providers are not profile-gated, but a pure
    # profile change is still a run that must not leave them behind.
    deploy_oidc_blueprints

    echo "Blueprints updated. Authentik will re-apply them automatically."

    # rc6.7 #94: re-apply policy bindings on profile toggle. Apps recreated
    # by `state: present` (after a previous `state: absent`) have no
    # bindings — without a group binding, the user has no policy match and
    # the tile is hidden from the App library even though the DB row
    # exists. Sleep first to let the Authentik worker pick up the
    # blueprint changes (the worker watches /blueprints with a debounced
    # interval) before the python script tries to find the apps.
    echo "Waiting 15s for Authentik worker to apply blueprint changes..."
    sleep 15
    wait_for_oidc_blueprints || true
    echo "Re-applying policy bindings..."
    # #1148: the rename runs on every path that reconciles bindings — it is a
    # no-op unless RAZZFAZZ_ADMIN_USERNAME differs from the bootstrap name.
    rename_bootstrap_admin
    apply_policy_bindings
    repair_managed_scope_mappings || true   # #2501, before the reconcile re-applies the system blueprint
    drop_gated_synapse_instance
    echo "Reconciling blueprint instances..."
    reconcile_blueprints
    # #2090: the binding pass above ran while the worker was still applying
    # blueprints — a group created a minute after the binding that names it is
    # a binding never made (`chat` bound only to Super Admins on 0.79,
    # `llm-management` bound to nothing on 0.91: an app with zero bindings is
    # OPEN to every authenticated user). Every instance has now been applied or
    # retried, so one more idempotent pass turns the race into a non-event.
    echo "Re-applying policy bindings after the blueprint reconcile (#2090)..."
    # #1148: the rename rides every binding pass (no-op once done).
    rename_bootstrap_admin
    apply_policy_bindings

    # Every path ends with the outpost verdict (reports, never fails).
    check_outpost_providers || true
    [ "${RECONCILE_FAILED:-0}" = "0" ] || { echo "ERROR: the module-binding / outpost reconcile was skipped or failed (see above) — authentik-init exits non-zero. Re-run: rzfz post-install --refresh"; exit 1; }
    echo "${COMPOSE_PROFILES:-}" > "${INIT_FLAG_FILE}.profiles"
    # #183: backfill/refresh the domain baseline too — we only reach this
    # branch when DOMAIN_CHANGED=false, so the current domain is still the
    # correct baseline to record for the next run's comparison.
    echo "${MAIN_DOMAIN:-}" > "${INIT_DOMAIN_FLAG_FILE}"
    echo "Profile-based app visibility updated."
    exit 0
fi

# Define paths (mapped inside the container)
SOURCE_BLUEPRINTS="/init/blueprints"
SOURCE_MEDIA="/init/media"
TARGET_BLUEPRINTS="/blueprints"
TARGET_MEDIA="/data/media"
# 2026.2+: brand assets (logo, flow background, app icons) must live at
# /data/media/public/. That is where BOTH Authentik's signed serving
# (/files/media/public/<file>?token=) and Caddy's public /branding route
# (root /srv/authentik-media/media/public) read them from. Placing them one
# level up at /data/public/ — as this did — leaves /data/media/public/ empty,
# so on a CLEAN install the login logo + background 404 (upgraded boxes only
# worked by accumulating a copy here across version hops).
TARGET_PUBLIC="/data/media/public"
mkdir -p "$TARGET_MEDIA" "$TARGET_PUBLIC"

# 1. Handle Media Files (Always copy base media). Ownership (1000:1000) and
# permissions ride inside copy_media_assets — R2: the fast path calls only the
# function, so a chown living out here never ran on upgrades.
copy_media_assets

# Wait for Authentik to fully stabilize (user requested 5m delay)
echo "Waiting 1.5 minutes (90s) for Authentik Server/Worker to stabilize..."
sleep 90

# 2. Handle Blueprints based on Scenario
echo "Installing tools..."
ensure_init_tools || echo "WARN(#147): init tools unavailable — blueprint templating/bindings may fail."

echo "Copying Base blueprints and substituting variables..."
# Copy directly to root blueprint folder
TARGET_CUSTOM="$TARGET_BLUEPRINTS"

# Set profile-based launch URLs (blank://blank hides the app from the Library)
# See: https://github.com/goauthentik/authentik/issues/1837
ACTIVE_PROFILES="${COMPOSE_PROFILES:-}"
HIDDEN_URL="blank://blank"

# Helper: check if a profile is active
profile_active() {
    echo "$ACTIVE_PROFILES" | tr ',' '\n' | grep -qx "$1"
}

# Chat → chat profile
if profile_active "chat"; then
    export CHAT_LAUNCH_URL="https://${OPENWEBUI_DOMAIN}"
else
    export CHAT_LAUNCH_URL="$HIDDEN_URL"
fi

# Dify → dify or workflow-automation profile  
if profile_active "dify" || profile_active "workflow-automation"; then
    export DIFY_LAUNCH_URL="https://${DIFY_DOMAIN}"
else
    export DIFY_LAUNCH_URL="$HIDDEN_URL"
fi

# Administration → monitor profile
if profile_active "monitor"; then
    export ADMIN_LAUNCH_URL="https://${KOMODO_DOMAIN}"
else
    export ADMIN_LAUNCH_URL="$HIDDEN_URL"
fi

# LLM Management → llm, llm-legacy, or llm-cpu profile
if profile_active "llm" || profile_active "llm-legacy" || profile_active "llm-cpu"; then
    export LLM_LAUNCH_URL="https://${GPUSTACK_DOMAIN}"
else
    export LLM_LAUNCH_URL="$HIDDEN_URL"
fi

# Gitea → gitea profile
if profile_active "gitea"; then
    export GIT_LAUNCH_URL="https://${GITEA_DOMAIN}"
else
    export GIT_LAUNCH_URL="$HIDDEN_URL"
fi

# LightRAG → lightrag profile
if profile_active "lightrag"; then
    export RAG_LAUNCH_URL="https://${LIGHTRAG_DOMAIN}"
else
    export RAG_LAUNCH_URL="$HIDDEN_URL"
fi

# Cognee → cognee profile
if profile_active "cognee"; then
    export COGNEE_LAUNCH_URL="https://${COGNEE_DOMAIN}/docs"
else
    export COGNEE_LAUNCH_URL="$HIDDEN_URL"
fi

# Docling → docling profile
if profile_active "docling"; then
    export DOCLING_LAUNCH_URL="https://docling.${MAIN_DOMAIN}/docs"
else
    export DOCLING_LAUNCH_URL="$HIDDEN_URL"
fi

# Stirling-PDF → stirling-pdf profile
if profile_active "stirling-pdf"; then
    export STIRLING_LAUNCH_URL="https://pdf.${MAIN_DOMAIN}"
else
    export STIRLING_LAUNCH_URL="$HIDDEN_URL"
fi

# AI Agents → agents profile
if profile_active "agents"; then
    export AGENTS_LAUNCH_URL="https://${AGENTS_DOMAIN}"
    export MY_AGENTS_LAUNCH_URL="https://${AGENTS_DOMAIN}/dashboard"
else
    export AGENTS_LAUNCH_URL="$HIDDEN_URL"
    export MY_AGENTS_LAUNCH_URL="$HIDDEN_URL"
fi

# Crawl4AI → crawl4ai profile
if profile_active "crawl4ai"; then
    export CRAWL4AI_LAUNCH_URL="https://crawl4ai.${MAIN_DOMAIN}/playground"
else
    export CRAWL4AI_LAUNCH_URL="$HIDDEN_URL"
fi

# Observability → observability profile
if profile_active "observability"; then
    export OBSERVABILITY_LAUNCH_URL="https://${OBSERVABILITY_DOMAIN}"
else
    export OBSERVABILITY_LAUNCH_URL="$HIDDEN_URL"
fi

# Paperclip → paperclip profile
if profile_active "paperclip"; then
    export PAPERCLIP_LAUNCH_URL="https://${PAPERCLIP_DOMAIN:-paperclip.${MAIN_DOMAIN}}"
else
    export PAPERCLIP_LAUNCH_URL="$HIDDEN_URL"
fi

# Hermes Agent → hermes profile
if profile_active "hermes"; then
    export HERMES_LAUNCH_URL="https://${HERMES_DOMAIN:-hermes.${MAIN_DOMAIN}}"
else
    export HERMES_LAUNCH_URL="$HIDDEN_URL"
fi

# Matrix (Element Web browser client) → matrix profile
if profile_active "matrix"; then
    export ELEMENT_WEB_LAUNCH_URL="https://${ELEMENT_WEB_DOMAIN:-element.${MAIN_DOMAIN}}"
else
    export ELEMENT_WEB_LAUNCH_URL="$HIDDEN_URL"
fi

# Paperless-ngx → paperless-ngx profile
if profile_active "paperless-ngx"; then
    export PAPERLESS_LAUNCH_URL="https://${PAPERLESS_DOMAIN:-paperless.${MAIN_DOMAIN}}"
else
    export PAPERLESS_LAUNCH_URL="$HIDDEN_URL"
fi

# Vaultwarden → vaultwarden profile
if profile_active "vaultwarden"; then
    export VAULTWARDEN_LAUNCH_URL="https://${VAULTWARDEN_DOMAIN:-vault.${MAIN_DOMAIN}}"
else
    export VAULTWARDEN_LAUNCH_URL="$HIDDEN_URL"
fi

# Infisical → infisical profile
if profile_active "infisical"; then
    export INFISICAL_LAUNCH_URL="https://${INFISICAL_DOMAIN:-infisical.${MAIN_DOMAIN}}"
else
    export INFISICAL_LAUNCH_URL="$HIDDEN_URL"
fi

# Onyx → onyx profile
if profile_active "onyx"; then
    export ONYX_LAUNCH_URL="https://${ONYX_DOMAIN:-onyx.${MAIN_DOMAIN}}"
else
    export ONYX_LAUNCH_URL="$HIDDEN_URL"
fi

# M028 — Start Portal (always-on, no profile gate; no STATE flag — never absent)
export START_LAUNCH_URL="https://${START_DOMAIN}"

# Crawl4AI → crawl4ai profile (rc6.7 #94 — blueprint state gating)
if profile_active "crawl4ai"; then
    export CRAWL4AI_LAUNCH_URL="https://crawl4ai.${MAIN_DOMAIN}/playground"
    export CRAWL4AI_BLUEPRINT_STATE="present"
else
    export CRAWL4AI_LAUNCH_URL="$HIDDEN_URL"
    export CRAWL4AI_BLUEPRINT_STATE="absent"
fi

echo "Profile-based launch URLs set (active profiles: $ACTIVE_PROFILES):"
echo "  Chat:    $CHAT_LAUNCH_URL"
echo "  Dify:    $DIFY_LAUNCH_URL"
echo "  Admin:   $ADMIN_LAUNCH_URL"
echo "  LLM:     $LLM_LAUNCH_URL"
echo "  Git:     $GIT_LAUNCH_URL"
echo "  RAG:     $RAG_LAUNCH_URL"

# Clean up existing files to ensure fresh copy
rm -f "$TARGET_BLUEPRINTS"/*.yaml 2>/dev/null || true
rm -rf "$TARGET_BLUEPRINTS"/razzfazz 2>/dev/null || true
rm -rf "$TARGET_BLUEPRINTS"/zz_razzfazz 2>/dev/null || true

for file in $SOURCE_BLUEPRINTS/base/*.yaml; do
    echo "Processing $file..."
    envsubst < "$file" > "$TARGET_CUSTOM/$(basename "$file")"
done
# #2501: 18-synapse-oidc.yaml only with the matrix profile and its credentials.
gate_synapse_blueprint
gate_openbao_blueprint

echo "Listing files in $TARGET_BLUEPRINTS after base copy:"
ls -R $TARGET_BLUEPRINTS

# Google/Entra SSO sources + the combined flow integration: deploy_sso_blueprints
# (defined above, #2794) — shared with the fast path, which runs it when the SSO
# inputs changed.
deploy_sso_blueprints

# #29/#30 native-OIDC providers — one function for every path (#2507).
deploy_oidc_blueprints

echo "Setting permissions..."
chown_new_blueprints

# Final listing of blueprints
echo "Final listing of blueprints in $TARGET_BLUEPRINTS:"
ls -R $TARGET_BLUEPRINTS

# 3. Apply System Settings directly via Python (Workaround for Blueprint limitations)
apply_tenant_avatars


# #2507: the OIDC providers apply asynchronously — wait for them before the
# binding pass, which binds their applications to Chat/Git Users + Super Admins.
wait_for_oidc_blueprints || true

# 4. Apply Access Policy Bindings (Python script)
echo "Applying Access Policy Bindings..."
# #1148: the rename runs on every path that reconciles bindings — it is a
# no-op unless RAZZFAZZ_ADMIN_USERNAME differs from the bootstrap name.
rename_bootstrap_admin
apply_policy_bindings

# #1350: the applies above were queued in bulk; whatever lost the race against
# outpost_send_update is still `error` and nothing else will retry it. #1370:
# and nobody would ever hear about it — BlueprintInstance has no error column,
# the reason lives only in the worker log where the deadlock lines bury it.
repair_managed_scope_mappings || true   # #2501, before the reconcile re-applies the system blueprint
drop_gated_synapse_instance
echo "Reconciling blueprint instances..."
reconcile_blueprints
# #2090: the binding pass above ran while the worker was still applying
# blueprints — a group created a minute after the binding that names it is
# a binding never made (`chat` bound only to Super Admins on 0.79,
# `llm-management` bound to nothing on 0.91: an app with zero bindings is
# OPEN to every authenticated user). Every instance has now been applied or
# retried, so one more idempotent pass turns the race into a non-event.
echo "Re-applying policy bindings after the blueprint reconcile (#2090)..."
# #1148: the rename rides every binding pass (no-op once done).
rename_bootstrap_admin
apply_policy_bindings

# 5. App visibility is now handled via blueprint envsubst (Step 2)
#    The meta_launch_url is set to blank://blank for inactive profiles
#    directly in the blueprint file during envsubst processing.
#    No separate Python script needed.

# ==============================================================================
# MARK INITIALIZATION AS COMPLETE
# ==============================================================================
# #2816: the baseline (version, profiles, DOMAIN) is the previously APPLIED
# state. It is written only after a run whose reconcile completed, so a failed
# run is detected as changed again on the next run instead of "unchanged".
# Every path ends with the outpost verdict (reports, never fails).
check_outpost_providers || true
[ "${RECONCILE_FAILED:-0}" = "0" ] || { echo "ERROR: the module-binding / outpost reconcile was skipped or failed (see above) — authentik-init exits non-zero. Re-run: rzfz post-install --refresh"; exit 1; }
echo "$INIT_VERSION" > "$INIT_FLAG_FILE"
rm -f "${INIT_FLAG_FILE}.started"   # #2403: the apply completed
# Store active profiles for change detection on next run
echo "${COMPOSE_PROFILES:-}" > "${INIT_FLAG_FILE}.profiles"
# #183: store MAIN_DOMAIN for change detection on next run (this full-reinit
# path just re-templated every blueprint with the CURRENT domain, so it's the
# correct new baseline).
echo "${MAIN_DOMAIN:-}" > "${INIT_DOMAIN_FLAG_FILE}"
record_sso_state   # #2794
echo "=============================================="
echo "Authentik initialization complete!"
echo "Flag file created: $INIT_FLAG_FILE"
echo "This script will not run again unless the flag is removed"
echo "or COMPOSE_PROFILES/MAIN_DOMAIN change."
echo "=============================================="