# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Agent type catalog — seeds and manages the available agent types."""

import json
import logging
import os

import psycopg2.extras

from app.services import llm_config
from app.services import mcp_config
from app.services import voice_config

logger = logging.getLogger(__name__)

# ── #36 coding-agent split — per-TYPE coding agents (opencode/gsd-pi/codex/
# user-defined) replacing the single bundled "coding-tools". All four share the
# same shared web terminal image family (razzfazz-coding-agent-<kind>) built
# from modules/agents/coding-agent-web via per-kind build args. Each gets its
# own per-user PERSISTENT named volume set (workspace + home) so cloned repos,
# user-installed tools/binaries, tmux resurrect saves, and config survive a
# container restart/recreate.
_CODING_SPLIT_PORTS = json.dumps({'internal': 3004, 'protocol': 'http'})

# Persistent volume set shared by all split coding agents. /workspace holds
# repos; the four /home/agent/.* dirs hold agent config, caches, the user's
# self-installed tools (~/.local, ~/.npm) and tmux resurrect saves — every path
# that must outlive a restart. Named per (type, user) by the provisioner.
_CODING_SPLIT_VOLUMES = json.dumps([
    {'name_suffix': 'workspace', 'mount': '/workspace'},
    # Whole home dir persisted — covers ~/.config (agent+own-key+gitea token),
    # ~/.local (self-installed binaries + tmux resurrect), ~/.npm, ~/.gsd,
    # ~/.codex, ~/.cache, ~/.git-credentials. One volume keeps it simple and
    # guarantees a user-installed tool (Phase 5) + its config persist.
    {'name_suffix': 'home', 'mount': '/home/agent'},
])


def _coding_split_env(kind: str, with_model: bool) -> str:
    """env_template for a split coding agent. with_model=False for user-defined
    (no LLM config at all)."""
    env = {
        'AGENT_KIND': kind,
        # #233 — the user's own name for this instance, so the terminal titles
        # itself with it instead of the generic kind. Resolved per instance in
        # provisioner._resolve_env, which DROPS this key when the user has not
        # set a name (an empty AGENT_LABEL would beat the image's own fallback).
        'AGENT_LABEL': '{{custom_name}}',
        # #2554: the owner's Gitea identity (provisioner records it at
        # launch); <slug>@<MAIN_DOMAIN> only when none was recorded.
        'GIT_AUTHOR_NAME': '{{git_name}}',
        'GIT_AUTHOR_EMAIL': '{{git_email}}',
        # C1 layer-3 (PR #84 review): the container's own owner-identity gate
        # (coding-agent-web/web/app.py `_identity_ok`) compares
        # make_user_slug(X-Authentik-Username) against this. A mis-set network
        # that reaches the container without Caddy forward_auth still can't get
        # a shell — no identity header (or a non-owner one) → 403.
        'AGENT_OWNER_SLUG': '{{user_slug}}',
        # ── Gitea checkout wiring (#165) ─────────────────────────────────────
        # The sandbox has no DNS for the box's public Gitea domain (and the box
        # CA isn't trusted), so a copy-pasted UI clone URL
        # (https://git.<domain>/…) fails "Could not resolve host". Only
        # http://gitea:3000 is reachable on the agent net. We hand the container:
        #   * MAIN_DOMAIN + GITEA_EXTERNAL_URL — the external forms the entrypoint
        #     rewrites (git config insteadOf) → the reachable internal host, so a
        #     `git clone <UI-URL>` transparently works.
        #   * GITEA_INTERNAL_URL — the reachable internal endpoint the rewrite +
        #     the web-UI Gitea helper target.
        #   * GITEA_API_TOKEN — a PER-USER token minted on provision (best-effort;
        #     see provisioner._mint_gitea_token). Never the shared admin token, so
        #     one user's sandbox can't reach another's repos. Empty when Gitea is
        #     off or the user hasn't logged into Gitea yet — the entrypoint then
        #     skips the credential block and the user can paste their own PAT in
        #     the "Clone from Gitea" UI (which GITEA_EXTERNAL_URL now surfaces).
        'MAIN_DOMAIN': '{{MAIN_DOMAIN}}',
        'GITEA_EXTERNAL_URL': '{{GITEA_EXTERNAL_URL}}',
        'GITEA_INTERNAL_URL': '{{GITEA_INTERNAL_URL}}',
        'GITEA_API_TOKEN': '{{gitea_token}}',
    }
    if with_model:
        env.update({
            'GPUSTACK_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_BASE_URL': 'http://llm:8080/v1',
            'LLM_MODEL': llm_config.model_for('coding-tools'),
            # Per-session model picker options (Phase 4) — full alias list from
            # standard-models.yaml so the New-session dialog offers every model.
            'AGENT_MODEL_ALIASES': json.dumps(llm_config.alias_list()),
        })
    if kind == 'user-defined':
        # Pre-wire Pi (pi.dev / @earendil-works/pi-coding-agent) to the local
        # model (#36 / PR #84). user-defined is a bare terminal with NO built-in
        # agent + NO per-session model picker (with_model=False above, so
        # AGENT_KIND=user-defined → AGENT_MODEL_CONFIG=0 in entrypoint.sh). But
        # Pi is a first-class suggestion in INSTALL_SUGGESTIONS, so we seed its
        # config file up front — `npm install -g @earendil-works/pi-coding-agent`
        # then `pi` talks to qwen3.6 via GPUStack out of the box (no OpenAI 401,
        # no cloud). Pi reads ~/.pi/agent/models.json — the SAME schema gsd uses
        # (Pi and gsd share lineage), so we reuse gsd_models_json() verbatim; the
        # entrypoint points its baseUrl at the local-model shim so
        # enable_thinking=false is injected (qwen3.x thinking-burn guard). These
        # env keys are needed by the entrypoint's shim + config seeding even
        # though the terminal itself stays model-agnostic.
        env.update({
            'GPUSTACK_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_BASE_URL': 'http://llm:8080/v1',
            'LLM_MODEL': llm_config.model_for('coding-tools'),
            'PI_MODELS_JSON': llm_config.gsd_models_json(),
        })
    if kind == 'gsd-pi':
        env['GSD_MODELS_JSON'] = llm_config.gsd_models_json()
    if kind == 'codex':
        # The entrypoint writes codex a model catalog (without one codex offers
        # the local model no file-edit tool); this is each model's context size.
        env['CODEX_MODEL_CONTEXT_JSON'] = json.dumps(llm_config.context_windows())
    if kind == 'opencode':
        env['OPENCODE_CONFIG_JSON'] = json.dumps({
            **json.loads(llm_config.opencode_config_json(
                default_alias=llm_config.model_for('coding-tools'),
            )),
            'mcp': mcp_config.opencode_mcp_block(),
        })
    return json.dumps(env)


# ── #244-Q4 / Rework-W1: which pane(s) an agent type renders ────────────────
# Explicit per-type, with a ports-based fallback for types not listed (a new
# type with an internal HTTP port gets web+terminal until curated). Kept as
# CODE, not a DB column: the classification is a property of the TYPE's
# software, not per-box state — a schema field would just be one more thing
# the seed/upsert dance can drift on.
# ── W5 (#620, EPIC #244 item 8): quick-start presets ─────────────────────────
# Curated one-click launches for non-technical users: pick an "app", never a
# model or a memory size. Code-level like UI_KINDS — a preset is product
# curation, not user data; company-shareable preset SHARING is the Stack-Apps
# epic (2026.11). `config` is resolved SERVER-SIDE in /api/launch-preset so a
# tampered client cannot smuggle arbitrary launch config through a preset id.
# `agent_type` must be an enabled catalog type at call time or the preset is
# hidden/refused.
PRESETS = [
    {
        "id": "coding-standard",
        "label": "Coding agent",
        "description": "opencode with the box's local model — start coding "
                       "against your Gitea repos in one click.",
        "agent_type": "opencode",
        "config": {},
    },
    {
        "id": "claude-code-ready",
        "label": "Bring your own agent",
        "description": "A bare sandbox terminal prepared for npm-installed "
                       "agents (Claude Code, Pi, dsh) — git and the local "
                       "model are pre-wired.",
        "agent_type": "user-defined",
        "config": {},
    },
    {
        "id": "personal-assistant",
        "label": "Personal assistant",
        "description": "Hermes with dashboard, memory and chat — your "
                       "always-on personal agent.",
        "agent_type": "hermes",
        "config": {},
    },
]


def launchable_presets(catalog, enabled_ids=None):
    """W5 (#620) / #1186: the quick-start presets whose agent_type is enabled
    on this box, in the public shape (`id`/`label`/`description`/
    `agent_type` — never `config`, which is resolved server-side at launch).

    ONE source for both `GET /api/presets` and the server-rendered launch
    band on /agents (#1186: the tiles used to be injected by a post-paint
    fetch, shifting the page on every click). `enabled_ids` lets a caller
    that already holds the enabled type list skip a second catalog read.
    """
    if enabled_ids is None:
        enabled_ids = {t['id'] for t in catalog.get_types(enabled_only=True)}
    return [
        {'id': p['id'], 'label': p['label'], 'description': p['description'],
         'agent_type': p['agent_type']}
        for p in PRESETS if p['agent_type'] in enabled_ids
    ]


def preset_by_id(preset_id: str):
    """Resolve a preset or None. Single source for the launch endpoint."""
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None


UI_KINDS = {
    # web-first agents: their own UI is the product; a raw shell is secondary
    "moltis": "web",
    "paperclip": "web",
    # dashboard AND TUI/gateway
    "hermes": "both",
    # coding agents: their web UI is a terminal-manager (sessions, clone,
    # ports) AND the manager PTY works — both panes are first-class
    "coding-tools": "both",
    "opencode": "both",
    "gsd-pi": "both",
    "codex": "both",
    "user-defined": "both",
}


def ui_kind_for(type_row: dict) -> str:
    """'terminal' | 'web' | 'both' for an agent-type row."""
    kind = UI_KINDS.get(type_row.get("id") or "")
    if kind:
        return kind
    ports = type_row.get("ports")
    if isinstance(ports, str):
        try:
            ports = json.loads(ports)
        except ValueError:
            ports = {}
    return "both" if (ports or {}).get("internal") else "terminal"


#: #2482 — agent types whose portal pane loads from the instance's own
#: subdomain (`<type>-<token>.<AGENTS_DOMAIN>`), not from `/i/<token>/`.
#: Criterion: the app's SPA writes ROOT-ABSOLUTE document paths and has no
#: base-path setting — Moltis 20260827.01: router.ts history.pushState(path) /
#: mount(location.pathname), ws-connect.ts `${location.host}/ws/chat` and
#: location.assign('/login'). Hermes is NOT here: it honours X-Forwarded-Prefix
#: (#2481). Adding a type is a framing decision: see
#: proxy.apply_instance_frame_policy.
SUBDOMAIN_PANE_TYPES = frozenset({'moltis'})


#: #2476 — the uid of the process user in each agent image the stack builds.
#: The Agent Manager mounts an owner-only tmpfs at /run/rzfz for it, where the
#: owner's short-lived OpenBao token is written (never env, argv or image).
#: A type not listed (paperclip: an upstream image) gets no mount,
#: and the token broker then delivers nothing.
SECRETS_TMPFS_UID = {
    'codex': 1000, 'opencode': 1000, 'gsd-pi': 1000, 'user-defined': 1000,
    'coding-tools': 1000,
    'hermes': 10000,
    'moltis': 1000,
}


#: #2717 — agent types that can run the Claude Code CLI: Paperclip ships it in
#: its image, and every coding-agent image lets the user install it into the
#: persistent home. The provisioner sets CLAUDE_CODE_ENV in their primary
#: container on every create and recreate. DISABLE_ERROR_REPORTING stops
#: Claude Code's own error reports and nothing else: feature flags, Remote
#: Control, auto-update and model traffic stay on (DISABLE_TELEMETRY and
#: CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC would also switch off feature-flag
#: fetching, and with it Claude Code features). There is no per-type override: a type's
#: env_template cannot switch it off. It is not stale wiring: a running agent
#: without it is never force-recreated, it picks it up on its next recreate.
CLAUDE_CODE_TYPES = frozenset({
    'coding-tools', 'opencode', 'gsd-pi', 'codex', 'user-defined', 'paperclip',
})
CLAUDE_CODE_ENV = {'DISABLE_ERROR_REPORTING': '1'}
#: #2717 — Claude Code's telemetry intake (Claude Code network-config docs:
#: "Operational telemetry events" and "Operational error reports"). The
#: provisioner pins both to 0.0.0.0 in the primary container of every
#: CLAUDE_CODE_TYPES instance (docker `extra_hosts`), on launch and recreate.
#: Exact names: the second is its own domain, not under datadoghq.com. Not
#: stale wiring: a running agent without them is never force-recreated. The
#: allowlist-mode egress proxy denies the same names
#: (modules/agents/egress-proxy/Caddyfile; kept equal by a test).
CLAUDE_CODE_BLOCKED_HOSTS = (
    'http-intake.logs.us5.datadoghq.com',
    'browser-intake-us5-datadoghq.com',
)


# Catalog seed data — defines the five agent types per the M011 spec
SEED_TYPES = [
    {
        'id': 'hermes',
        'display_name': 'Hermes (Personal)',
        'tier': 'lightweight',
        # ── Option B (#36, PR #84): SINGLE container per instance. ────────────
        # Since hermes v0.16 the FIRST-PARTY built-in dashboard is a full admin
        # surface (channels / MCP / credentials / webhooks / memory / gateway /
        # chat / skills), so the separate third-party hermes-workspace Next.js
        # companion (github.com/outsourc-e/hermes-workspace) — the source of the
        # persistent "agent not connected" bug — is redundant and was DROPPED.
        # One container per instance now: agent-hermes-<slug>, serving the
        # built-in dashboard on port 9119 (the routed UI) + the messaging
        # gateway on 8642 (the OpenWebUI-pipe target).
        #
        # rc6.7 #56 — built locally from the public
        # github.com/NousResearch/hermes-agent at the pinned tag (the upstream
        # GHCR image is private, HTTP 403 anonymous). Built by the
        # `hermes-agent-image` service in agents/compose.yml.
        'image': 'razzfazz-stack-hermes-agent',
        # Pinned away from `latest` to an immutable release tag so
        # docker-volume-backup snapshots, razzfazz-checksum.sh governance, and
        # offline upgrade packages are deterministic. Bump via the
        # `check-and-bump-versions` skill, not by re-pointing at `latest`.
        # v2026.9.24 (== hermes v0.21.5) — 2026.11 GA update with a smoke test
        # (modules/agents/hermes-agent/smoke-test.sh). Prior:
        # v2026.8.27 — routine currency bump (chore/bump-agents-hermes-moltis,
        # operator-verified latest release tag). Prior: v2026.8.3 == hermes
        # v0.20.0 (security roll-up: gateway credential-isolation, DoS/ReDoS
        # #76083, JWKS #75437, token-leak #60199); before that v2026.7.1 ==
        # v0.18.0. v0.18 moved to an s6-overlay-supervised image (see
        # modules/agents/hermes-agent/Dockerfile) and made the dashboard auth
        # gate MANDATORY on non-loopback binds — satisfied by the
        # HERMES_DASHBOARD_BASIC_AUTH_* env below. This is a catalog-only pin:
        # it affects newly-provisioned per-user instances; existing instances
        # keep their currently-provisioned version until re-provisioned.
        'version': 'v2026.9.24',
        # Ports: the built-in dashboard (user-facing UI) on the catalog's
        # `internal` slot so Caddy + the manager proxy register a route to it —
        # 9119 in v0.18 (was 3000/workspace under the dropped companion). Agent
        # gateway on `agent_internal` (unchanged 8642) — the OpenWebUI hermes
        # pipe (M020 S03) targets it via
        # find('hermes', __user__, use_extra_port='agent_internal').
        'ports': json.dumps({
            'internal': 9119,
            'agent_internal': 8642,
            'protocol': 'http',
        }),
        # Primary container (hermes-agent) — persistent memory volume only.
        # M030-S1 confirmed: hermes-agent only writes user data to /opt/data
        # (Dockerfile VOLUME /opt/data; runtime FS audit on dev shows SOUL.md,
        # .env, cache, bin all under /opt/data; /opt/hermes is image-layer
        # source code; no /home/hermes dir exists). Catalog already maps
        # /opt/data as named — M030 compliant without changes.
        'volumes': json.dumps([{'name_suffix': 'agent', 'mount': '/opt/data'}]),
        'env_template': json.dumps({
            'HERMES_HOME': '/opt/data',
            # API_SERVER_KEY is the bearer token the OpenWebUI pipe uses
            # against the agent gateway. M020 S01 /api/find returns it as
            # auth.token; the value lives in instance.config['API_SERVER_KEY']
            # (provisioner stores `_generated_secret` under this key — see
            # provisioner change in this slice).
            'API_SERVER_HOST': '0.0.0.0',
            'API_SERVER_KEY': '{{generated_secret}}',
            # rc6.7 #60: hermes-agent's CLI defaults to interactive mode
            # and crash-loops without a TTY. API_SERVER_ENABLED switches
            # the `gateway` subcommand into the API-server path that the
            # OpenWebUI hermes pipe targets on agent_internal:8642.
            'API_SERVER_ENABLED': 'true',
            # rc6.7 #72: the upstream hermes gateway exits 1 on startup
            # if neither GATEWAY_ALLOW_ALL_USERS nor any platform allowlist
            # (TELEGRAM_ALLOWED_USERS, MATRIX_ALLOWED_USERS, …) is
            # configured — it considers "no allowlist" a fatal config:
            #   "No user allowlists configured. All unauthorized users
            #    will be denied."
            # In a per-user agent the inbound auth is already enforced at
            # the Authentik forward-auth + workspace UI layer (only the
            # one provisioning user can reach the gateway over the docker
            # network), so allowing "all" inside the container is the
            # right scope.
            'GATEWAY_ALLOW_ALL_USERS': 'true',
            'GPUSTACK_API_KEY': '{{GPUSTACK_API_KEY}}',
            'GPUSTACK_BASE_URL': 'http://llm:8080/v1',
            # rc6.7 #73: pre-configure GPUStack as the LLM provider so the
            # operator doesn't have to run `hermes setup` from the workspace
            # UI on first launch. hermes-agent's litellm-style LLM client
            # falls back to OPENAI_API_KEY + OPENAI_BASE_URL when no provider
            # has been explicitly selected in ~/.hermes/config.yaml — the
            # endpoint we hand it here is the same OpenAI-compatible
            # GPUStack URL we already pass for skills/tools above.
            'OPENAI_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_BASE_URL': 'http://llm:8080/v1',
            # M031 S1: model resolved from core/llm/standard-models.yaml at
            # agent-manager boot. Operator-side edits to the YAML +
            # `docker restart agent-manager` propagate to newly-provisioned
            # agents without a code or image change.
            'LLM_MODEL': llm_config.model_for('hermes'),
            'PYTHONUNBUFFERED': '1',
            # ── v0.18 built-in dashboard (Option B, #36) ──────────────────────
            # v0.18 runs the dashboard as an s6-overlay service inside the same
            # container, enabled by HERMES_DASHBOARD=1 (see the dashboard/run
            # script in the hermes-agent image).
            #
            # BIND STRATEGY (validated on 0.91 — this is the load-bearing bit):
            # v0.18's June-2026 hardening made the auth gate MANDATORY on a
            # NON-loopback (0.0.0.0) bind and REMOVED the --insecure bypass. Its
            # only zero-IDP option is the bundled basic-auth provider — but a
            # SINGLE password-only provider is BROKEN upstream: the middleware
            # auto-initiates an OAuth redirect to /auth/login?provider=basic,
            # which the password-only provider raises NotImplementedError on →
            # the dashboard root 500s ("Internal Server Error") in a browser.
            # A LOOPBACK bind, by contrast, runs the auth gate as a NO-OP (the
            # SPA gets an ephemeral session token injected into its HTML) and
            # the dashboard opens straight to the chat + admin surface. That's
            # the correct posture here anyway: the dashboard is ALREADY fully
            # fenced by Authentik forward-auth + the agent-manager owner-slug
            # check, so hermes' own redundant gate is pure (broken) friction.
            #
            # But a loopback-bound dashboard also enforces a DNS-rebind
            # Host-header check that 400s any non-loopback Host — and the
            # agent-manager proxy dials this container over docker DNS
            # (agent-hermes-<slug>). So we bind the dashboard on 127.0.0.1:9118
            # (auth off) and run a tiny stdlib Host-rewriting reverse proxy
            # (docker/hermes-dash-proxy.py, baked into the image) on 0.0.0.0:9119
            # that rewrites the Host to 127.0.0.1:9118 before forwarding. The
            # manager proxy reaches :9119 normally; the dashboard sees a loopback
            # Host + loopback bind → serves with no gate. (Started by the boot
            # `command` below.)
            'HERMES_DASHBOARD': '1',
            'HERMES_DASHBOARD_HOST': '127.0.0.1',
            'HERMES_DASHBOARD_PORT': '9118',
            'HERMES_DASH_PROXY_PORT': '9119',
            # In-browser Chat tab — embeds `hermes --tui` over PTY/WebSocket so
            # the dashboard is a full chat surface (chat replies come from the
            # LOCAL model configured by the boot `command` below).
            'HERMES_DASHBOARD_TUI': '1',
        }),
        # ── v0.18 single-container boot (Option B, #36) ──────────────────────
        # v0.18 is s6-overlay-supervised: /init is PID 1, the DASHBOARD runs as
        # its OWN s6 service (enabled by HERMES_DASHBOARD=1 in env_template
        # above — no longer background-started here), and this `command` runs as
        # /init's "main program" via docker/main-wrapper.sh. So the command only
        # needs to (1) seed the LLM config + MCP servers, then (2) `exec hermes
        # gateway run` as the pipe target on 8642. The dashboard (9119) comes up
        # independently under s6; the container exits when the gateway exits.
        #
        # main-wrapper.sh already: cd /opt/data, activates the venv, drops to the
        # hermes user (s6-setuidgid), and exec's our argv. `hermes` on PATH is
        # the privilege-drop shim → venv binary (a no-op short-circuit once
        # we're already the hermes user), so the absolute venv path and the bare
        # `hermes` both resolve to the same CLI.
        #
        # LLM config (rc6.7 #91): pre-seed config.yaml with our GPUStack target
        # so a fresh provisioning chats end-to-end without the operator running
        # `hermes setup`. hermes' config.yaml model.{default,provider,base_url}
        # override the OPENAI_* env, and the upstream default is provider=auto +
        # base_url=openrouter.ai — a fresh container without this hits
        # openrouter.ai (HTTP 401) despite OPENAI_* env. Keys:
        #   model.default  — bare model name ($LLM_MODEL, from standard-models.yaml).
        #   model.provider — `custom` = hermes' generic OpenAI-compat path.
        #   model.base_url — the GPUStack OpenAI-compatible endpoint (docker DNS).
        # `config set` is idempotent — safe on every boot; dashboard-UI edits
        # persist (set touches only the one key). stage2-hook seeds config.yaml
        # from the example on FIRST boot; these three run AFTER it (main program
        # runs after cont-init) so they always win the model.* keys.
        # M031 S1: model.default reads $LLM_MODEL (resolved via llm_config).
        #
        # v0.18 CRITICAL (validated on 0.91, #36): the `custom` provider reads
        # its API key from config.yaml `model.api_key`, NOT from the OPENAI_API_KEY
        # env the way v0.14 did. Without `model.api_key`, the gateway reaches
        # GPUStack but every chat 401s ("Invalid authentication credentials").
        # `config set model.api_key ${GPUSTACK_API_KEY}` is the v0.18 fix —
        # empirically a chat then returns from the LOCAL qwen3.6 model.
        # #959 D2: model.base_url/model.api_key are TEMPLATED placeholders
        # (not the literal gpustack URL/var) because #612's env-var rewrite
        # can't reach a value baked into a shell command — the provisioner's
        # command-resolution step (`_resolve_command_llm_placeholders`) fills
        # these in: gpustack box -> unchanged gpustack URL + ${GPUSTACK_API_KEY}
        # (byte-identical to pre-#959); llm-manager box (a per-user key was
        # minted) -> the manager endpoint + the minted key.
        'command': ['sh', '-c',
                    'hermes config set model.default "${LLM_MODEL:-qwen3.6}" 2>/dev/null; '
                    'hermes config set model.provider custom 2>/dev/null; '
                    'hermes config set model.base_url {{LLM_BASE_URL}} 2>/dev/null; '
                    'hermes config set model.api_key "{{LLM_API_KEY}}" 2>/dev/null; '
                    # #2087: voice through the box's Speaches — never Edge
                    # (Hermes' cloud default), never the LLM key.
                    + "".join(c + "; " for c in voice_config.hermes_voice_commands())
                    # M033 S12: register the cognee-mcp sidecar as a remote MCP
                    # server (memory backend; tools remember/recall/forget). URL
                    # form — cognee-mcp is reachable at cognee-mcp:8000/mcp on
                    # the agent network and isn't itself gated, so no auth header
                    # is needed (the sidecar holds the cognee API key). Idempotent
                    # M035 P1: registry-driven — `hermes mcp add` for every
                    # eligible MCP server from core/mcp/mcp-servers.yaml (was a
                    # hardcoded cognee line). Each command is `|| true` so a
                    # re-run or absent profile doesn't abort boot. Empty when no
                    # servers eligible.
                    + "".join(c + "; " for c in mcp_config.hermes_mcp_add_commands()) +
                    # #36 gap 2 — per-user personal MCP proxies (commit-review
                    # CRITICAL #2: NO shell eval of cross-service data). The
                    # provisioner sets RAZZFAZZ_USER_MCP_JSON (a JSON array of
                    # {id,url,headers}); we parse it in Python and exec `hermes
                    # mcp add` with a quoted argv LIST via subprocess
                    # (shell=False), so a crafted id/url/header is inert data,
                    # never a command. #61 CRITICAL-1: each proxy is bearer-gated
                    # — the spec's headers ({"Authorization":"Bearer <secret>"})
                    # are appended as `--header "K: V"` argv items so only this
                    # user's hermes can reach the proxy. Idempotent (check=False);
                    # empty/absent var is a no-op.
                    "python3 -c 'import json,os,subprocess; "
                    "[subprocess.run([\"/opt/hermes/.venv/bin/hermes\",\"mcp\",\"add\","
                    "str(s[\"id\"]),\"--url\",str(s[\"url\"])]"
                    "+[a for k,v in (s.get(\"headers\") or {}).items() "
                    "for a in (\"--header\",str(k)+\": \"+str(v))],check=False) "
                    "for s in json.loads(os.environ.get(\"RAZZFAZZ_USER_MCP_JSON\") or \"[]\")]' "
                    "2>/dev/null || true; "
                    # v0.18: the dashboard is a separate s6 service (see
                    # env_template HERMES_DASHBOARD=1), bound to loopback:9118.
                    # Start the Host-rewriting reverse proxy (0.0.0.0:9119 ->
                    # 127.0.0.1:9118) in the background so the manager proxy can
                    # reach the loopback dashboard over docker DNS without
                    # tripping its DNS-rebind Host check (see env_template note).
                    'python3 /opt/hermes/docker/hermes-dash-proxy.py '
                    '> /tmp/hermes-dash-proxy.log 2>&1 & '
                    # The main program is the gateway (the 8642 OpenWebUI-pipe
                    # target); the container exits when it exits.
                    'exec hermes gateway run'],
        # Option B (#36): the third-party hermes-workspace companion is DROPPED —
        # its companion_command / companion_env_template (config.yaml seeding for
        # the Next.js UI's readActiveModel() gate, HERMES_API_URL/TOKEN wiring,
        # HOST/PORT/HERMES_PASSWORD) all went away with it. The built-in v0.18
        # dashboard reads the SAME /opt/data/config.yaml the gateway container
        # writes (they're one container now), so no cross-container config-seed
        # is needed; the boot `command` above sets model.* directly.
        'requires_db': False,
        'requires_docker_socket': False,
        # Single container now (was ~250 MB agent + ~150 MB workspace). The
        # gateway + built-in dashboard together sit well under this; dropped from
        # 512m (which covered both containers) toward ~320m for the one.
        'mem_limit': '320m',
        'cpu_limit': 2.0,
        # M020 SPEC: persistent-by-design backends pay for being kept warmer.
        # Bumped from 1800 (30 min) → 14400 (4 h). Cold-start warm-up after
        # 4h idle is acceptable (~5–15 s).
        'idle_timeout': 14400,
        'enabled': True,
        'description': 'Self-improving Python AI agent by NousResearch with a built-in admin dashboard (chat, memory browser, skills, MCP, credentials, webhooks, gateway).',
        # #36: per-agent-type icon. Served by Caddy's branding_static snippet on
        # AGENTS_DOMAIN (root /srv/authentik-media/media/public); the asset ships
        # in core/Authentik/media/ and is uploaded to that volume on init.
        'icon_url': '/branding/razzfazz-ai_hermes_icon.png',
    },
    {
        'id': 'moltis',
        'display_name': 'Moltis',
        'tier': 'lightweight',
        # rc6.7 #56 — moved from ghcr.io/moltis-org/moltis (org slug
        # returns HTTP 404 anonymously — no public package published) to a
        # locally-built image cloned from the public
        # github.com/moltis-org/moltis at the pinned dated tag. Built by
        # the `moltis-image` service in agents/compose.yml.
        'image': 'razzfazz-stack-moltis',
        # Pinned away from `latest` to an immutable date-tag so per-user
        # provisioned moltis containers are reproducible. Bump via
        # `check-and-bump-versions`. Upstream ships ~daily date-tags.
        # 20261023.01 — routine currency bump (2026.11-rc6; the repo also
        # carries a parallel v0.10.x semver tag series which we deliberately do
        # NOT track — our build pins the dated release scheme). Prior:
        # 20261019.01. Catalog-only pin: affects newly-provisioned per-user
        # instances only; existing instances keep their currently-provisioned
        # version until re-provisioned.
        'version': '20261023.01',
        'ports': json.dumps({'internal': 13131, 'protocol': 'http'}),
        # M030-S1: every persistent path declared as a NAMED volume so
        # `docker rm` of the container preserves user state and `upgrade()`
        # can reattach the same volumes to a new image. The Dockerfile
        # declares VOLUME for /home/moltis/.{moltis,config/moltis,npm}
        # — without explicit named mounts these become Docker-anonymous
        # volumes that get orphaned on container recreation. Pre-M030
        # catalog had only /data + /config (which moltis doesn't actually
        # use — confirmed via FS inspection 2026-05-12); those are dropped.
        'volumes': json.dumps([
            # Chats (moltis.db SQLite WAL), agent memory (memory.db),
            # code-index (RAG-style code search), metrics database.
            # PRIMARY user data — losing this wipes weeks of chat history.
            {'name_suffix': 'home-moltis', 'mount': '/home/moltis/.moltis'},
            # User config: moltis.toml (per-user TOML overrides incl. the
            # M030 timeout raise), provider_keys.json (LLM credentials).
            {'name_suffix': 'home-config-moltis', 'mount': '/home/moltis/.config/moltis'},
            # npm cache. Not strictly user data (rebuildable), but kept
            # named so cold-start doesn't re-download the world on every
            # upgrade — saves ~1-2min per `upgrade()` invocation.
            {'name_suffix': 'home-npm', 'mount': '/home/moltis/.npm'},
            # M030-S3 (Option B per Q2 decision): shared volume that the
            # parent moltis AND its spawned sandbox containers mount. All
            # ephemeral sandbox FS state lives here so backup-walk can
            # capture it via the parent's volume set. Requires moltis-side
            # config to point sandbox FS into /shared (verified in S3).
            {'name_suffix': 'shared', 'mount': '/shared'},
        ]),
        'env_template': json.dumps({
            'MOLTIS_IDENTITY__NAME': 'moltis-{{user_slug}}',
            'MOLTIS_PASSWORD': '{{generated_secret}}',
            'MOLTIS_TLS__ENABLED': 'false',
            # #2482 C6 item 5c: Moltis marks its session cookie `Secure` only
            # when its own listener is TLS or, with MOLTIS_BEHIND_PROXY set,
            # when the proxy says `X-Forwarded-Proto: https`
            # (crates/httpd/src/auth_routes.rs should_secure_cookie, 20260827.01).
            # Caddy sets that header on the https instance host and the
            # manager forwards it, so this flag makes `moltis_session` Secure.
            # It also makes Moltis read X-Forwarded-Host/-For as the client's
            # (stricter locality: a proxied request is never "local") and is
            # only valid with its own TLS off (MOLTIS_TLS__ENABLED above).
            'MOLTIS_BEHIND_PROXY': 'true',
            # rc6.7 #73: pre-configure GPUStack as the LLM provider so the
            # operator doesn't have to point moltis at an LLM in the
            # browser flow on first launch. moltis reads its LLM config
            # from /home/moltis/.config/moltis/moltis.toml at startup;
            # the OPENAI_* env fallback covers it pre-config.
            #
            # 2026-05-12: switched moltis default model gemma4 → qwen3-
            # coder-next after moltis 20260510.01 update. gemma4's chat
            # template uses `value['type'] | upper` (a Jinja filter) on
            # tool-parameter type fields, which fails with
            #   "Unknown (built-in) filter 'upper' for type Array"
            # when a tool schema has `"type": ["string","null"]`
            # (JSON Schema 2020-12 nullable form). moltis 20260510's 61-
            # tool kit triggers this on the first chat. qwen-derived
            # templates handle array-form types correctly. Operator can
            # override per-instance via moltis Settings → Models if a
            # specific model is preferred.
            # 2026-05-12: model rename qwen3-coder-next → qwen3.6 (the
            # alias was retired; what was qwen3-coder-next on dev was always
            # the qwen3.6 deployment underneath). All consumer configs were
            # updated in the same pass — see .claude/skills/propagate-llm-config.
            # M031 S1: model resolved from standard-models.yaml.
            'OPENAI_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_BASE_URL': 'http://llm:8080/v1',
            'LLM_MODEL': llm_config.model_for('moltis'),
            'GPUSTACK_API_KEY': '{{GPUSTACK_API_KEY}}',
            'GPUSTACK_BASE_URL': 'http://llm:8080/v1',
            # #2482: Moltis builds its provider from OPENAI_API_KEY /
            # OPENAI_BASE_URL as `openai`; a provider's `models` list is what
            # its picker offers first. The old key named provider `local`,
            # which Moltis does not have, so the picker showed only its stock
            # GPT-4o. Alias list and default from standard-models.yaml (M031);
            # the list holds running chat models only (llm_config._chat_models).
            'MOLTIS_PROVIDERS__OPENAI__BASE_URL': 'http://llm:8080/v1',
            'MOLTIS_PROVIDERS__OPENAI__MODELS': json.dumps(llm_config.alias_list()),
            'MOLTIS_CHAT__PRIORITY_MODELS': json.dumps([llm_config.model_for('moltis')]),
            # 2026-05-12: raise per-agent-loop runtime limit from default 600s
            # to 1800s (30 min). The default trips on long agent runs that
            # write large files (research reports, multi-step skill workflows)
            # — operator hit it repeatedly during the news-report skill work.
            # Setting maps to defaults.toml [tools] agent_timeout_secs (env
            # form: MOLTIS_<SECTION>__<KEY> per moltis' double-underscore
            # nesting convention).
            'MOLTIS_TOOLS__AGENT_TIMEOUT_SECS': '1800',
            # #2661: moltis gets NO docker socket (see requires_docker_socket
            # below), so it must not go looking for a container runtime.
            # Measured upstream at the pinned tag (moltis-org/moltis
            # 20260827.01): the socket serves only the exec-tool sandbox
            # backend (crates/tools/src/sandbox/router.rs auto_detect_backend:
            # docker iff `docker info` works) and the browser tool follows the
            # same per-session decision (crates/tools/src/browser.rs
            # `router.is_sandboxed`), insisting on a container runtime when it
            # is set (crates/browser/src/pool.rs launch_sandboxed_browser).
            #   mode=off → tools run inside this container (the container is
            #     the sandbox, #2423), the browser uses the image's Chromium,
            #     and exec keeps moltis' own approval gating for host commands.
            #   backend=restricted-host → a per-session "sandbox on" toggle in
            #     the moltis UI runs exec through restricted-host instead of
            #     probing for docker. The BROWSER tool in such a session still
            #     takes its container branch (pool.rs launch_sandboxed_browser)
            #     and fails safely with "No container runtime available" —
            #     there is no socket to reach.
            # Env form per moltis' MOLTIS_<A>__<B> → [a] b rule
            # (crates/config/src/loader/config_io.rs apply_env_overrides).
            'MOLTIS_TOOLS__EXEC__SANDBOX__MODE': 'off',
            'MOLTIS_TOOLS__EXEC__SANDBOX__BACKEND': 'restricted-host',
            # M033 S12: wire the cognee-mcp sidecar as a memory backend via
            # moltis' double-underscore env nesting (same mechanism as
            # MOLTIS_TOOLS__ above) → moltis.toml [mcp.servers.cognee]. Tools
            # exposed: remember / recall / forget (validated live against
            # cognee/cognee-mcp:main 2026-05-24). Reaches the shared sidecar at
            # http://cognee-mcp:8000/mcp (streamable-http). When the `cognee`
            # profile is disabled the sidecar is absent and moltis simply marks
            # this MCP server unavailable — non-fatal. The env→[mcp.servers.*]
            # nested-table mapping follows the verified MOLTIS_PROVIDERS__/
            # MOLTIS_TOOLS__ pattern.
            # M035 P1: registry-driven — every eligible MCP server from
            # core/mcp/mcp-servers.yaml is wired here (was a hardcoded cognee
            # block). mcp_config reads COMPOSE_PROFILES for requires_profile
            # filtering. Emits MOLTIS_MCP__SERVERS__<ID>__{TRANSPORT,URL}.
            **mcp_config.moltis_mcp_env(),
            # #2087: voice through the box's Speaches.
            **voice_config.moltis_voice_env(),
        }),
        # M030-S2 Q6: pre-stop hook to flush moltis SQLite WAL before
        # `docker stop`, otherwise an upgrade run during an active write
        # could leave moltis.db-wal in a state that requires recovery on
        # next start (usually safe, occasionally not). Honored by
        # provisioner.upgrade() and stop(). Run inside the container with
        # a short timeout — if it hangs we still stop the container after
        # the timeout, just without the clean flush.
        #
        # Implementation note: the moltis image ships python3 + libsqlite3
        # but NOT the sqlite3 CLI binary, so we use Python's built-in
        # sqlite3 module to issue the WAL checkpoint. Glob /home/moltis/.moltis
        # for any *.db (covers moltis.db, memory.db, metrics.db, code-index/*).
        'pre_stop_command': [
            'python3', '-c',
            'import glob, sqlite3\n'
            'for p in glob.glob("/home/moltis/.moltis/**/*.db", recursive=True):\n'
            '    try:\n'
            '        c = sqlite3.connect(p, timeout=5)\n'
            '        c.execute("PRAGMA wal_checkpoint(FULL)")\n'
            '        c.close()\n'
            '    except Exception as e:\n'
            '        print(f"checkpoint {p}: {e}")\n'
        ],
        'pre_stop_timeout': 10,
        'requires_db': False,
        # #2661 (SECURITY): was True since the type was added (M011) — the raw
        # host /var/run/docker.sock, read-write, in every per-user instance:
        # root on the box for whoever drives moltis (its owner via moltis' web
        # terminal, or a prompt-injected agent loop). Moltis only used it for
        # its optional exec sandbox, switched off in env_template above. No
        # type keeps a socket; the guard is tests/unit/agent-manager/test_2661_no_raw_docker_socket.py.
        'requires_docker_socket': False,
        # rc6.7 #72: bumped from 256m. Steady-state usage is ~35 MB so
        # cgroup OOM was never the trigger, but under host memory pressure
        # (when multiple agents + GPUStack models are loaded together)
        # the kernel OOM-killer scored moltis high enough to pick it
        # (Exit 137 with OOMKilled=false). Lifting the limit raises moltis'
        # `oom_score_adj` floor and gives it the same kill priority as
        # the other lightweight agents.
        'mem_limit': '512m',
        'cpu_limit': 1.0,
        'idle_timeout': 1800,
        'enabled': True,
        'description': 'Rust personal agent server with Matrix, Telegram, and Discord gateway.',
        'icon_url': '/branding/razzfazz-ai_moltis_icon.png',
    },
    {
        'id': 'coding-tools',
        'display_name': 'Coding Tools',
        'tier': 'lightweight',
        'image': 'razzfazz-coding-tools',
        'version': 'latest',
        # rc6.7 #74: revert M020 S06's port change. M020 S06 moved the
        # user-facing port from 3004 (the python web terminal that serves
        # gsd + opencode in an in-browser xterm) to 8080 (gsd --web, a
        # native Next.js UI for gsd specifically). gsd --web in v2.78.1
        # binds to 127.0.0.1 (the spawned Next.js standalone reads
        # process.env.HOSTNAME which Docker auto-sets to the container ID
        # → it falls back to 127.0.0.1; the --hostname 0.0.0.0 flag we
        # passed doesn't propagate down). Result: the agent-manager proxy
        # can't reach :8080 and returns 502 to the user. Keeping the
        # opencode pipe target on :4096 unchanged.
        #
        # The python web terminal at /opt/coding-tools-web/app.py on
        # :3004 is the working UI — xterm in the browser, type `gsd` or
        # `opencode` for the interactive TUI. That's what users want
        # anyway (the M020 S06 plan note that "gsd-2 web UI per-user
        # (no chat integration)" was an idea, not a requirement).
        # gsd --web stays running in the background for now (idempotent
        # if listening on 127.0.0.1 only — won't conflict) until the
        # entrypoint stops launching it in a follow-up cleanup slice.
        'ports': json.dumps({
            'internal': 3004,
            'opencode_internal': 4096,
            'protocol': 'http',
        }),
        # M030-S1: every persistent path declared as a NAMED volume so
        # `docker rm` of the container preserves user state. The Dockerfile
        # creates /home/agent/.{gsd,config/opencode,local/share/opencode,
        # local/state/opencode,cache/opencode} and chowns them to agent:agent
        # so volume mounts inherit ownership. Pre-M030 catalog only declared
        # /workspace as named — the opencode + gsd state dirs were Docker-
        # anonymous volumes (because of the mkdir + image-layer copy pattern)
        # and got orphaned on container recreation, losing opencode session
        # history, gsd state, and the model cache.
        'volumes': json.dumps([
            # User code, repos, opencode session working dirs
            {'name_suffix': 'workspace', 'mount': '/workspace'},
            # gsd-pi state (~/.gsd) — task lists, agent definitions
            {'name_suffix': 'gsd', 'mount': '/home/agent/.gsd'},
            # opencode config (~/.config/opencode) — model registry, providers
            {'name_suffix': 'opencode-config', 'mount': '/home/agent/.config/opencode'},
            # opencode shared data (~/.local/share/opencode) — sessions, tools
            {'name_suffix': 'opencode-share', 'mount': '/home/agent/.local/share/opencode'},
            # opencode runtime state (~/.local/state/opencode) — checkpoints
            {'name_suffix': 'opencode-state', 'mount': '/home/agent/.local/state/opencode'},
            # opencode cache (~/.cache/opencode) — model file cache (large)
            {'name_suffix': 'opencode-cache', 'mount': '/home/agent/.cache/opencode'},
        ]),
        'env_template': json.dumps({
            'GIT_AUTHOR_NAME': '{{git_name}}',
            'GIT_AUTHOR_EMAIL': '{{git_email}}',
            # M020 S05 — opencode serve auth + boot flags
            'OPENCODE_SERVER_PASSWORD': '{{generated_secret}}',
            'OPENCODE_SERVE_AT_BOOT': 'true',
            # M020 S06 — gsd --web surfaces per-user
            'GSD_WEB_AT_BOOT': 'true',
            # rc6.7 #73: pre-configure GPUStack as the LLM provider for
            # both opencode (writes ~/.local/share/opencode/config from
            # OPENAI_API_KEY/OPENAI_BASE_URL) and gsd (reads
            # ~/.gsd/agent/models.json which the entrypoint writes from
            # GPUSTACK_API_KEY/OPENAI_BASE_URL — see entrypoint.sh).
            # M031 S1: model resolved from standard-models.yaml.
            'GPUSTACK_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_BASE_URL': 'http://llm:8080/v1',
            'LLM_MODEL': llm_config.model_for('coding-tools'),
            # M031 S2: gsd's models.json + opencode's config.json are now
            # generated from standard-models.yaml on the agent-manager side
            # and passed as full JSON env vars. entrypoint.sh writes them
            # to disk verbatim — no hardcoded models or context numbers.
            # Operator-side YAML edit + `docker restart agent-manager`
            # propagates to newly-provisioned coding-tools agents on next
            # entrypoint run (idempotent: only writes if not already on disk).
            'GSD_MODELS_JSON': llm_config.gsd_models_json(),
            # M035 P1: merge the registry's MCP servers into opencode's config
            # under the `mcp` key (opencode reads ~/.config/opencode/config.json).
            'OPENCODE_CONFIG_JSON': json.dumps({
                **json.loads(llm_config.opencode_config_json(
                    default_alias=llm_config.model_for('coding-tools'),
                )),
                'mcp': mcp_config.opencode_mcp_block(),
            }),
        }),
        'requires_db': False,
        'requires_docker_socket': False,
        # M020 S05 — bumped from 1536m to 2048m to accommodate
        # opencode serve + gsd --web + interactive shell budget.
        # rc6.7 #72: 2048m still triggers the OOM-killer in practice with
        # all four processes warm (opencode serve, gsd --web, gsd headless,
        # interactive shell budget), exit 137 on the dev box. Bump to 4g —
        # consistent with the pattern of "per-user heavy workloads need
        # roughly 2x the shared-profile budget" since each user spawns
        # their own copy.
        'mem_limit': '4g',
        'cpu_limit': 2.0,
        'pids_limit': 2048,  # #221 — coding family lifted from the 512 default
        'idle_timeout': 1800,
        'description': 'Persistent coding workspace with opencode serve (chat) + gsd --web (UI).',
        # coding-tools uses the shared "coding" branding asset (razzfazz-ai_coding_icon.png).
        'icon_url': '/branding/razzfazz-ai_coding_icon.png',
        # #36: coding-tools is SUPERSEDED by the per-type split below
        # (opencode / gsd-pi / codex / user-defined). Kept enabled=False so
        # existing provisioned instances still resolve their type_info for
        # start/stop/delete, but it no longer shows as a launchable card.
        'enabled': False,
        # D7 (#2518): `enabled=False` alone made a still-running instance of
        # this type INVISIBLE — /api/tree only ever walked
        # `get_types(enabled_only=True)`, so a group for a disabled type was
        # never built and its rows (the operator's own recreated coding-tools
        # instance) silently vanished from the portal tree. `deprecated` is a
        # distinct, explicit marker: /api/tree now includes a disabled/
        # deprecated type's group whenever the caller still has an instance
        # of it, badges it "deprecated: migrate or delete", and leaves start/
        # stop/delete/upgrade working — it only stops offering NEW launches
        # (already true via `enabled=False`). `replaced_by` is a hint the
        # badge shows so the operator knows where to migrate.
        'deprecated': True,
        'replaced_by': ['opencode', 'codex', 'user-defined'],
    },
    # ── #36 coding-agent split — per-TYPE coding agents ──────────────────────
    {
        'id': 'opencode',
        'display_name': 'opencode',
        'tier': 'lightweight',
        'image': 'razzfazz-coding-agent-opencode',
        'version': 'latest',
        'ports': _CODING_SPLIT_PORTS,
        'volumes': _CODING_SPLIT_VOLUMES,
        'env_template': _coding_split_env('opencode', with_model=True),
        'requires_db': False,
        'requires_docker_socket': False,
        # #36 security-review — hostile-container sandbox: coding-agents net
        # only (egress-controlled), cap_drop ALL, read-only root + tmpfs,
        # pids_limit, no host bind-mounts, no docker socket, non-root.
        'sandbox': True,
        'mem_limit': '2g',
        'cpu_limit': 2.0,
        # #221 — coding agents run MCP servers (Cognee, Playwright) + multiple
        # agent processes; threads count toward the pids cgroup and the 512
        # hardening default was exhausted in the field. Lift the sandboxed
        # coding family to 2048 (still bounded fork-bomb protection); every
        # other agent type keeps docker_client's 512 default.
        'pids_limit': 2048,
        'idle_timeout': 1800,
        'enabled': True,
        # #959 D3: was "the local GPUStack model" — hardcoded the gpustack
        # backend into user-facing copy even on an LLM-Manager box (#612/#959
        # already switch the ACTUAL endpoint; the label just hadn't caught
        # up). "Local model" is backend-neutral; dashboard.py appends
        # " · metered via LLM Manager" when this box's active backend is the
        # LLM Manager, so the label never asserts GPUStack it isn't using.
        'description': 'opencode AI coding agent — multi-session tmux web terminal, '
                       'defaults to a local model, per-session model/auth picker.',
        'icon_url': '/branding/razzfazz-ai_opencode_icon.png',
    },
    {
        # #36 FEATURE 2 (PR #84): gsd-pi is RETIRED as a dedicated agent. The
        # operator wants gsd-pi to stop being its own provisionable card/icon and
        # instead be a user-installable CLI in the User Defined Agent (its
        # INSTALL_SUGGESTIONS now offers `npm install -g @opengsd/gsd-pi`,
        # alongside native pi). Kept as a catalog row with enabled=False so any
        # pre-existing provisioned gsd-pi instance can still resolve its type_info
        # for stop/delete during deprovisioning, but it no longer renders as a
        # launchable card (get_types(enabled_only=True) drops it) and it's removed
        # from every quota tier's allowed_types below. The per-type image builder
        # (coding-agent-gsd-pi-image) is likewise removed from agents/compose.yml.
        'id': 'gsd-pi',
        'display_name': 'gsd-pi',
        'tier': 'lightweight',
        'image': 'razzfazz-coding-agent-gsd-pi',
        'version': 'latest',
        'ports': _CODING_SPLIT_PORTS,
        'volumes': _CODING_SPLIT_VOLUMES,
        'env_template': _coding_split_env('gsd-pi', with_model=True),
        'requires_db': False,
        'requires_docker_socket': False,
        'sandbox': True,
        'mem_limit': '2g',
        'cpu_limit': 2.0,
        # #221 — coding agents run MCP servers (Cognee, Playwright) + multiple
        # agent processes; threads count toward the pids cgroup and the 512
        # hardening default was exhausted in the field. Lift the sandboxed
        # coding family to 2048 (still bounded fork-bomb protection); every
        # other agent type keeps docker_client's 512 default.
        'pids_limit': 2048,
        'idle_timeout': 1800,
        'enabled': False,
        # D7 (#2518): same explicit marker as coding-tools — see its comment.
        'deprecated': True,
        'replaced_by': ['user-defined'],
        'description': 'gsd-pi coding agent — RETIRED as a dedicated agent; '
                       'install it inside a User Defined Agent '
                       '(npm install -g @opengsd/gsd-pi).',
        'icon_url': '/branding/razzfazz-ai_gsd_icon.png',
    },
    {
        'id': 'codex',
        'display_name': 'Codex',
        'tier': 'lightweight',
        'image': 'razzfazz-coding-agent-codex',
        'version': 'latest',
        'ports': _CODING_SPLIT_PORTS,
        'volumes': _CODING_SPLIT_VOLUMES,
        # Codex (Apache-2.0 binary shipped in the image). Local model via
        # ~/.codex/config.toml [model_providers.gpustack] pointing at a local
        # Responses→Chat shim with wire_api="responses" (written + started by
        # entrypoint.sh — codex dropped wire_api="chat" and GPUStack is
        # Chat-Completions-only; the shim translates. See #36 / PR #84).
        'env_template': _coding_split_env('codex', with_model=True),
        'requires_db': False,
        'requires_docker_socket': False,
        'sandbox': True,
        'mem_limit': '2g',
        'cpu_limit': 2.0,
        # #221 — coding agents run MCP servers (Cognee, Playwright) + multiple
        # agent processes; threads count toward the pids cgroup and the 512
        # hardening default was exhausted in the field. Lift the sandboxed
        # coding family to 2048 (still bounded fork-bomb protection); every
        # other agent type keeps docker_client's 512 default.
        'pids_limit': 2048,
        'idle_timeout': 1800,
        'enabled': True,
        # #959 D3: see the opencode description above — same GPUStack→neutral
        # label fix.
        'description': 'OpenAI Codex CLI (Apache-2.0) — multi-session tmux web terminal, '
                       'defaults to a local model, per-session model/auth picker '
                       '(Own subscription = codex login --device-auth).',
        'icon_url': '/branding/razzfazz-ai_codex_icon.png',
    },
    {
        'id': 'user-defined',
        'display_name': 'User Defined Agent',
        'tier': 'lightweight',
        'image': 'razzfazz-coding-agent-user-defined',
        'version': 'latest',
        'ports': _CODING_SPLIT_PORTS,
        'volumes': _CODING_SPLIT_VOLUMES,
        # NO model config — a bare persistent terminal. The web UI shows a
        # curated copy-paste list of suggested installers (Claude Code, Codex,
        # Gemini CLI, Aider, Cursor CLI); we ship none of them. The user's
        # self-installed agent + its config persist via the home/workspace vols.
        'env_template': _coding_split_env('user-defined', with_model=False),
        'requires_db': False,
        'requires_docker_socket': False,
        'sandbox': True,
        # #238: 2g (and even the 4g some instances were raised to) is too low —
        # prod cgroup-OOM-killed two concurrent Claude Code sessions here
        # (~1.5-2 GB each under esbuild) while PID 1 survived, so the agent
        # looked healthy with its sessions silently gone. Node/Claude-hosting
        # agents need headroom for two concurrent sessions plus the shell.
        'mem_limit': '8g',
        'cpu_limit': 2.0,
        # #221 — coding agents run MCP servers (Cognee, Playwright) + multiple
        # agent processes; threads count toward the pids cgroup and the 512
        # hardening default was exhausted in the field. Lift the sandboxed
        # coding family to 2048 (still bounded fork-bomb protection); every
        # other agent type keeps docker_client's 512 default.
        'pids_limit': 2048,
        'idle_timeout': 1800,
        'enabled': True,
        'description': 'A bare, persistent multi-session terminal — bring your own agent. '
                       'Curated install one-liners (Claude Code, Codex, Gemini CLI, Aider, …) '
                       'are suggested in the UI; nothing is pre-installed.',
        'icon_url': '/branding/razzfazz-ai_userdefined_icon.png',
    },
    {
        'id': 'paperclip',
        'display_name': 'Paperclip',
        'tier': 'heavy',
        'image': 'razzfazz-stack-paperclip',
        'version': 'latest',
        'ports': json.dumps({'internal': 3100, 'protocol': 'http'}),
        # rc6.7 #77: paperclip writes its persistent state (config.json,
        # agent JWT, DB backups) to PAPERCLIP_DATA_DIR=/paperclip/.paperclip
        # — the env var below + the upstream image's HOME=/paperclip
        # convention. The previous mount at /app/data was a stale carry-over
        # and never matched what paperclip actually wrote, so every
        # container restart wiped config.json (postgres records survived,
        # but the local agent JWT was gone) and the user got
        # "Agent JWT missing — run `pnpm paperclipai onboard`" on the
        # banner with no way to recover without re-running the wizard.
        #
        # M030-S1: paperclip is already M030-compliant. Bulk state lives
        # in the per-instance Postgres DB (DATABASE_URL → instance_db);
        # local file state (JWT, config.json, DB backups) lives in
        # /paperclip/.paperclip which IS named-mounted. The Dockerfile's
        # VOLUME ["/paperclip"] declaration creates an anonymous volume
        # for the OUTER /paperclip dir but PAPERCLIP_DATA_DIR redirects
        # all paperclip writes into the named .paperclip subdir, so the
        # outer anonymous volume only ever holds image-layer code (which
        # is correctly refreshed on upgrade). No additional named volumes
        # needed — verified against rc6.7 #77 fix + upstream paperclip
        # convention.
        'volumes': json.dumps([{'name_suffix': 'data', 'mount': '/paperclip/.paperclip'}]),
        'env_template': json.dumps({
            'DATABASE_URL': 'postgresql://{{instance_db_user}}:{{instance_db_password}}@postgres/{{instance_db_name}}',
            'VALKEY_URL': 'redis://:{{VALKEY_PASSWORD}}@valkey/{{valkey_db_index}}',
            'HOST': '0.0.0.0',
            'PAPERCLIP_DEPLOYMENT_MODE': 'authenticated',
            'BETTER_AUTH_SECRET': '{{generated_secret}}',
            # M031-FOLLOWUPS B1+B2: subdomain MUST use {{instance_hash}} to match
            # Caddy's per-instance route (caddy_client.instance_token).
            # AGENT_INSTANCE_HOSTNAME
            # is consumed by entrypoint-wrapper.sh to register the hostname in
            # paperclip's allowed-hostname list at startup (no manual `pnpm
            # paperclipai allowed-hostname` dance per fresh instance).
            'PAPERCLIP_PUBLIC_URL': 'https://paperclip-{{instance_hash}}.agents.{{MAIN_DOMAIN}}',
            'AGENT_INSTANCE_HOSTNAME': 'paperclip-{{instance_hash}}.agents.{{MAIN_DOMAIN}}',
            'PAPERCLIP_DATA_DIR': '/paperclip/.paperclip',
            'GPUSTACK_API_KEY': '{{GPUSTACK_API_KEY}}',
            'GPUSTACK_BASE_URL': 'http://llm:8080/v1',
            # rc6.7 #73: also expose OPENAI_* aliases so paperclip's
            # internal LLM client (separate from the bundled opencode
            # config below) and the BetterAuth-protected admin API both
            # see GPUStack as the default OpenAI-compatible endpoint.
            # M031 S1: model resolved from standard-models.yaml.
            'OPENAI_API_KEY': '{{GPUSTACK_API_KEY}}',
            'OPENAI_BASE_URL': 'http://llm:8080/v1',
            'LLM_MODEL': llm_config.model_for('paperclip'),
            # M031 S2: paperclip's bundled opencode config is generated from
            # standard-models.yaml on the agent-manager side. Operator-side
            # YAML changes (new model alias, display-name edit) propagate
            # on the next paperclip provision without touching catalog.py.
            'OPENCODE_GPUSTACK_CONFIG': llm_config.opencode_provider_block(),
        }),
        'requires_db': True,
        'requires_docker_socket': False,
        # Bumped from 512m — paperclip is a Node.js + Next.js app bundling
        # Better-Auth, Postgres client, plugin-tool-dispatcher, plugin-loader,
        # plugin-job-coordinator, plugin-job-scheduler, automatic DB-backup
        # worker, and a heartbeat client. 512m got SIGKILL'd by the OOM
        # killer immediately after the first GET / 200 (#14: per-user
        # paperclip 502, exit 137, OOMKilled=true).
        'mem_limit': '2g',
        'cpu_limit': 1.5,
        'idle_timeout': 7200,
        'enabled': True,
        'description': 'AI company orchestration — hire AI agents, set goals, automate workflows.',
        'icon_url': '/branding/razzfazz-ai_paperclip_icon.png',
    },
]

# Default quota tiers per §7.1
SEED_TIERS = [
    {
        'id': 'agent-basic',
        'display_name': 'Basic',
        'max_per_type': 1,
        'max_heavy': 0,
        # #36: coding-tools superseded by the per-type split.
        # FEATURE 2 (PR #84): gsd-pi retired as a dedicated agent — dropped from
        # the allow-list (it's now a User Defined Agent install suggestion).
        'allowed_types': json.dumps(['hermes', 'moltis',
                                     'opencode', 'codex', 'user-defined']),
        'priority': 0,
    },
    {
        'id': 'agent-power',
        'display_name': 'Power',
        'max_per_type': 1,
        'max_heavy': 1,
        'allowed_types': None,  # null = all types
        'priority': 10,
    },
    {
        'id': 'agent-admin',
        'display_name': 'Admin',
        'max_per_type': 3,
        'max_heavy': 3,
        'allowed_types': None,
        'priority': 99,
    },
]


class AgentCatalog:
    def __init__(self, db):
        self._db = db
        self._seed()

    def _seed(self):
        """Seed agent types and quota tiers — always upsert to pick up image/version changes.

        #511: `preserve_admin_fields=True` exempts the four fields the admin
        console can set (`enabled`, `mem_limit`, `cpu_limit`, `idle_timeout`).
        This runs on EVERY agent-manager start, so without the exemption an
        operator's disable or raised mem_limit survived only until the next
        upgrade / reboot / `compose up -d`. A brand-new agent type is an
        INSERT and still gets all of its seed defaults.
        """
        for type_data in SEED_TYPES:
            self._db.upsert_agent_type(type_data, preserve_admin_fields=True)
            logger.info(f"Seeded/updated agent type: {type_data['id']}")

        for tier_data in SEED_TIERS:
            existing = self._db.get_quota_tier(tier_data['id'])
            if not existing:
                self._db.upsert_quota_tier(tier_data)
                logger.info(f"Seeded quota tier: {tier_data['id']}")

    def get_types(self, enabled_only=True):
        return self._db.get_agent_types(enabled_only=enabled_only)

    def get_type(self, type_id: str):
        return self._db.get_agent_type(type_id)
