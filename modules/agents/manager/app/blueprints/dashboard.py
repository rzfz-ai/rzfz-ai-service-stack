# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""User-facing dashboard — agent instance cards and management UI."""

import os
import socket
import threading
import time

from app.services.catalog import launchable_presets
from urllib.parse import urlparse

from flask import Blueprint, current_app, render_template, request, url_for

from app.services.ingress import ingress_ok, log_refusal
from app.services.provisioner import (instance_display_name, make_user_slug,
                                      slug_candidates)

dashboard_bp = Blueprint('dashboard', __name__)


# --- source-IP anchor (#397) -------------------------------------------------
# The dashboard derives `make_user_slug(...)` straight from
# `X-Authentik-Username`, so a direct dial with a forged header renders an
# arbitrary user's instance list and instance detail. Same anchor as `api_bp`
# (#390) and `admin_bp`.
@dashboard_bp.before_request
def require_ingress():
    if ingress_ok():
        return None
    log_refusal('dashboard', request.method, request.path, request.remote_addr)
    return render_template('error.html', message='Forbidden.'), 403

# M020 S08 — agent_type → OpenWebUI pipe model id (for "Open in Chat" links).
# Maps to the manifold pipes shipped in M020 S03/S04/S05.
_CHAT_MODEL_FOR_TYPE = {
    'hermes':       'hermes.personal',
    'moltis':       'moltis.personal',
    'coding-tools': 'opencode.personal',
}

def _chat_model_for_type(agent_type: str) -> str | None:
    return _CHAT_MODEL_FOR_TYPE.get(agent_type)


# #959 D3 — the catalog's per-type `description` was hardcoding "the local
# GPUStack model" even on an LLM-Manager box (#612/#959 already repoint the
# ACTUAL container endpoint; only this user-facing copy hadn't caught up).
# The catalog text was neutralised to "a local model"; here we append this
# suffix ONLY when the box's active backend really is the LLM Manager, so
# the label never asserts a backend the box isn't running. Detected the same
# way provisioner._mint_llm_manager_key does (an `llm-manager` container
# exists) — no per-user key needed for a read-only label.
_LLM_MANAGER_LABEL_SUFFIX = ' · metered via LLM Manager'

# #1186 (finding 2): the `/agents` page is a full server render on EVERY
# click of the "Agents" nav item — nothing paints until the handler returns.
# Everything below is therefore kept off the request path: the two backend
# probes are cached with a short TTL, and (see _build_dashboard_context) the
# per-agent docker stats sample moved to a client-side fetch. A dockerd that
# is merely busy (image pull, many containers) answers `inspect` in hundreds
# of ms; a page that does one inspect per running agent before its first
# byte turns that into seconds of frozen old page → blank → new page, which
# is exactly the "flickers and takes very long" the operator saw on 0.91.
# rzfz review #1210: `ts` must start at -inf, not 0.0 — time.monotonic() is the
# host UPTIME on Linux, so within the first TTL seconds after boot `now - 0.0 <
# TTL` held and the probe returned its default WITHOUT probing.
_LLM_MANAGER_PROBE = {'ts': float('-inf'), 'active': False}
_LLM_MANAGER_PROBE_TTL = 30.0


def _llm_manager_active() -> bool:
    """True iff an `llm-manager` container exists on this box. One docker
    inspect per _LLM_MANAGER_PROBE_TTL, never one per render."""
    now = time.monotonic()
    if now - _LLM_MANAGER_PROBE['ts'] < _LLM_MANAGER_PROBE_TTL:
        return _LLM_MANAGER_PROBE['active']
    try:
        active = bool(current_app.docker_client.get_container_state('llm-manager'))
    except Exception:
        active = False
    _LLM_MANAGER_PROBE['ts'] = now
    _LLM_MANAGER_PROBE['active'] = active
    return active


# MCP-manager reachability probe, cached. MCP_MANAGER_URL is set
# unconditionally by the compose default (modules/agents/compose.yml), so its
# mere presence is NOT a reliable "mcp profile active" signal. The truthful
# signal is whether the mcp-manager container actually accepts a connection:
# with the `mcp` profile off the container does not exist and the TCP connect
# fails (DNS/refused).
#
# #1186: the probe runs in a BACKGROUND thread (stale-while-revalidate). The
# old in-request `socket.create_connection(timeout=0.5)` bounded only the
# connect; the DNS lookup inside it has no timeout at all, so on a box whose
# upstream resolver is slow or dead every 30s the next /agents render stalled
# for the resolver's full timeout. A render now waits at most
# _MCP_PROBE_WAIT for a fresh answer and otherwise serves the last known one
# (False until the first probe lands). Under gunicorn's gevent worker the
# thread is a greenlet and getaddrinfo goes through gevent's resolver, so
# the probe never blocks other requests either.
_MCP_PROBE = {'ts': float('-inf'), 'active': False, 'thread': None}
_MCP_PROBE_TTL = 30.0
_MCP_PROBE_WAIT = 0.6


def _tcp_open(base) -> bool:
    """One TCP connect to the host:port in `base`. Never raises."""
    try:
        u = urlparse(base)
        host = u.hostname
        port = u.port or (443 if u.scheme == 'https' else 80)
        if not host:
            return False
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except Exception:
        return False


def _mcp_reachable(base):
    """True iff the mcp-manager at `base` accepted a TCP connection on the
    last completed probe. Result cached for _MCP_PROBE_TTL seconds; a stale
    entry is refreshed in the background. Never raises, never blocks a
    request for longer than _MCP_PROBE_WAIT."""
    if not base:
        return False
    now = time.monotonic()
    if now - _MCP_PROBE['ts'] < _MCP_PROBE_TTL:
        return _MCP_PROBE['active']
    t = _MCP_PROBE['thread']
    if t is None or not t.is_alive():
        def _run():
            active = _tcp_open(base)
            _MCP_PROBE['active'] = active
            _MCP_PROBE['ts'] = time.monotonic()
        t = threading.Thread(target=_run, name='mcp-probe', daemon=True)
        _MCP_PROBE['thread'] = t
        t.start()
    # A healthy box answers (refused / NXDOMAIN / accepted) in milliseconds,
    # so the common case still sees a fresh value; only a hung resolver
    # falls through to the stale one.
    t.join(_MCP_PROBE_WAIT)
    return _MCP_PROBE['active']


def _reset_probe_caches():
    """Test hook: forget both cached probe answers (each test builds its own
    app with its own docker mock and must not inherit the previous answer)."""
    _LLM_MANAGER_PROBE.update(ts=0.0, active=False)
    _MCP_PROBE.update(ts=0.0, active=False, thread=None)


def _mcp_context():
    """#959 Task 1.6 — shared MCP-gating logic, extracted so both the card
    dashboard and the portal's rail nav read the same signal.

    Gated on the mcp-manager being *reachable*, not merely on MCP_MANAGER_URL
    being set (the compose default always sets it); the link targets
    mcp.<domain>/dashboard, SSO-gated by the same "AI Agents Users" group.
    """
    main_domain = os.environ.get('MAIN_DOMAIN', 'localhost')
    mcp_active = _mcp_reachable(os.environ.get('MCP_MANAGER_URL', '').strip())
    mcp_url = f"https://mcp.{main_domain}/dashboard"
    return mcp_active, mcp_url


def _build_dashboard_context():
    username = request.headers.get('X-Authentik-Username', 'anonymous')
    user_slug = make_user_slug(username)
    groups = [g.strip() for g in request.headers.get('X-Authentik-Groups', '').split('|') if g.strip()]
    main_domain = os.environ.get('MAIN_DOMAIN', 'localhost')
    chat_base = f"https://chat.{main_domain}"

    agent_types = current_app.catalog.get_types()
    instances = current_app.db.get_user_instances(user_slug)
    # #1988: one card per INSTANCE, not per type - the tier promises several
    # of a type and the schema allows them now. `instance_map` keeps the first
    # instance per type for the callers that still mean "the" one (deep links).
    by_type = {}
    for i in sorted(instances, key=lambda r: (r['agent_type'], r.get('instance_no') or 1)):
        by_type.setdefault(i['agent_type'], []).append(i)
    instance_map = {t: rows[0] for t, rows in by_type.items()}
    # resolve_user_tier returns a synthetic unlimited tier for super-admin
    # group members (so admins aren't subject to quotas).
    tier = current_app.db.resolve_user_tier(groups)

    # #36 / PR #84 — memory governance surface. The memory picker is only shown
    # to power/admin tiers (the SERVER enforces it too — the UI just reflects
    # the tier gate). Presets + per-instance max come from the provisioner so a
    # single source of truth drives both dialog and settings page.
    from app.services.provisioner import _tier_allows_custom_memory, MEM_PRESETS_GB
    can_set_memory = _tier_allows_custom_memory(tier)
    mem_presets = list(MEM_PRESETS_GB)
    mem_default_gb = current_app.provisioner._default_mem_gb()
    mem_max_gb = current_app.provisioner._per_instance_max_gb()

    # #959 D3 — computed once per render, applied per-card below.
    llm_manager_active = _llm_manager_active()

    cards = []
    for at in agent_types:
      for instance in (by_type.get(at['id']) or [None]):
          allowed_types = tier.get('allowed_types') if tier else []
          type_allowed = (tier and (allowed_types is None or at['id'] in allowed_types))
          heavy_ok = (at['tier'] != 'heavy' or (tier and tier['max_heavy'] > 0))

          # #1186: NO docker stats sample here any more. PR #84 had already cut
          # the per-container cost from ~1s to ~2ms with one_shot, but the
          # inspect + stats pair per RUNNING agent still sat between the click
          # and the first byte — and scales with the agent count and with how
          # busy dockerd is. The card renders a skeleton meter and the page
          # fills it from ONE batched GET /api/stats after first paint.

          # M020 S08 — "Open in Chat" deep link for backend types that have a
          # corresponding OpenWebUI pipe (hermes, moltis, coding-tools).
          chat_model = _chat_model_for_type(at['id'])
          chat_url = f"{chat_base}/?models={chat_model}" if chat_model else None

          # Per-instance "Open" URL — uses the opaque-token subdomain
          # registered by caddy_client (post-2026-05-11 commit 79e85bbe).
          # Computed here so the template doesn't have to know about token
          # derivation. None for non-running instances (button stays disabled).
          open_url = None
          if instance and instance['state'] == 'running':
              open_url = current_app.caddy_client.get_instance_url(
                  at['id'], instance['user_slug'], instance_id=instance['id']
              )

          # M030-S2: "Update available" detection. Compare instance's running
          # image_version to the catalog's current version for this agent
          # type. Only surface badge + Update button when both values are
          # known AND differ — instances pre-S2 have NULL image_version
          # (treated as "version unknown" → no badge, since we can't tell
          # if there's actually a newer one). Once an instance is launched
          # OR upgraded post-S2, image_version is accurate from then on.
          update_available = False
          update_target_version = None
          if instance and instance.get('image_version') and at.get('version'):
              if instance['image_version'] != at['version']:
                  update_available = True
                  update_target_version = at['version']

          # #36 / PR #84 — the per-instance memory limit chosen at launch
          # (persisted on config['mem_limit']), for the "usage vs limit" line on
          # the card. Falls back to the catalog default when unset (pre-governance
          # instances / never-raised).
          mem_limit_str = None
          if instance:
              icfg = instance.get('config') or {}
              if isinstance(icfg, str):
                  import json as _json
                  try:
                      icfg = _json.loads(icfg)
                  except (ValueError, TypeError):
                      icfg = {}
              mem_limit_str = icfg.get('mem_limit') or at.get('mem_limit')

          # #959 D3 — append the "· metered via LLM Manager" suffix only to
          # types whose description actually mentions the local model AND only
          # when the box's active backend is confirmed to be the LLM Manager.
          # gpustack-only boxes get the neutral catalog text untouched.
          description = at.get('description') or ''
          if llm_manager_active and 'local model' in description.lower():
              description = description + _LLM_MANAGER_LABEL_SUFFIX

          cards.append({
              'type': at,
              'description': description,
              'instance': instance,
              # #233 — the user's own name for this agent when they set one.
              # Same helper the portal tree and /api/instances use, so the three
              # surfaces cannot drift.
              # `.get` rather than `[...]`: one odd catalog row must not 500 the
              # whole agents page — showing the type id is a better failure.
              'display_name': (instance_display_name(instance, at) if instance
                               else (at.get('display_name') or at.get('id'))),
              'can_launch': type_allowed and heavy_ok and not instance,
              'restricted': not type_allowed or not heavy_ok,
              'chat_url': chat_url,
              'open_url': open_url,
              'update_available': update_available,
              'update_target_version': update_target_version,
              'mem_limit': mem_limit_str,
          })

    total_instances = len(instances)

    # #1186: the launch band's quick-start tiles render server-side from the
    # SAME helper `/api/presets` returns — injecting them after first paint
    # shifted the whole page down by a tile row on every click. The enabled
    # type set is the one we already read above; no second catalog query.
    presets = launchable_presets(current_app.catalog,
                                 enabled_ids={at['id'] for at in agent_types})

    # M020 S08 — auto-action via query string (?launch=<type> | ?start=<id>).
    # Surfaces the current state to the template; client-side JS picks it up
    # and pre-fires the corresponding agentAction() call so the launch
    # dialog (or start spinner) opens immediately.
    auto_launch = request.args.get('launch') or ''
    auto_start = request.args.get('start') or ''

    # #36/#61 — MCP integrations surface. The combined "MCP & Agent Manager"
    # dashboard offers BOTH agent provisioning AND a link into the per-user
    # MCP dashboard. We show the MCP section only when the mcp profile is
    # active (MCP_MANAGER_URL is set — the same signal mcp_manager_client uses
    # for its fail-safe no-op). The link targets mcp.<domain>/dashboard, which
    # is SSO-gated by the same "AI Agents Users" group.
    #
    # DESIGN (#36): this is the linked-view unification — one app, one group,
    # one tile, one dashboard entry point that surfaces both functions. A deep
    # in-process merge of the MCP CRUD/OAuth UI into this dashboard is scoped
    # as a follow-up (two divergent Caddy clients / CSS themes / networks make
    # it a large refactor; see the PR description).
    mcp_active, mcp_url = _mcp_context()

    return dict(
        cards=cards, tier=tier,
        total_instances=total_instances, presets=presets,
        username=username,
        auto_launch=auto_launch, auto_start=auto_start,
        mcp_active=mcp_active, mcp_url=mcp_url,
        # #36 / PR #84 — memory picker context (tier-gated).
        can_set_memory=can_set_memory, mem_presets=mem_presets,
        mem_default_gb=mem_default_gb, mem_max_gb=mem_max_gb,
    )


@dashboard_bp.route('/agents')
@dashboard_bp.route('/dashboard/agents')   # back-compat deep-link
def index():
    """The card dashboard — the launch + provisioning surface.

    #959 finalisation: this is NO LONGER the landing. The redesigned Workspace
    (`portal()` at `/` and `/dashboard`) is what the Authentik tile opens now.
    The Workspace hands off launch/settings back here via
    `url_for('dashboard.index')`, which resolves to `/agents`.
    """
    return render_template('dashboard/index.html', **_build_dashboard_context())


@dashboard_bp.route('/')
@dashboard_bp.route('/dashboard')   # M020 S08 — Authentik launch URL; #959: now the Workspace
@dashboard_bp.route('/portal')      # kept so existing /portal deep-links still resolve
def portal():
    """The mission-control shell (#244-Q2/Q3): agent tree + one live pane.

    #959 finalisation: this redesigned Workspace is now the DEFAULT landing —
    `/` and `/dashboard` (the Authentik launch URL) both render it, so opening
    the Agent Manager tile lands on the signed-off new UI instead of the old
    card dashboard (which now lives at `/agents`).

    Deliberately thin. Everything that changes — which agents exist, what state
    they are in, whether one is actually serving — arrives from `/api/tree` and
    `/api/ready/<id>` client-side, because a portal you "live in" cannot make
    you reload the page to see a circle go green.

    The pane is same-origin (Q1): the terminal opens `/ws/terminal/<id>` on this
    origin and the web view iframes `/i/<token>/` on this origin. No per-agent
    subdomain is involved, which is what lets one page host an agent at all.

    Settings deliberately hand off to the existing per-instance settings page
    rather than being reimplemented here — it carries the memory picker and
    the type-to-confirm delete, and forking it would mean two places to keep
    a safety gate correct. Launching an agent (#959 Task C3) now happens
    inline: an empty slot's `.launcher` posts to the SAME `/api/launch/
    <agent_type>` route the spawn modal uses and attaches the result via the
    SAME `Slot.open(node)` the drag-drop flow uses — no separate launch path.
    """
    mcp_active, mcp_url = _mcp_context()
    return render_template('dashboard/portal.html',
                           username=request.headers.get('X-Authentik-Username',
                                                        'anonymous'),
                           mcp_active=mcp_active, mcp_url=mcp_url)


# #1986: where "← back" goes from an instance's settings page.
#
# The link used to be hardwired to the agents overview, but the page is reached
# from two places — the overview AND the Workspace, whose gear button is built in
# api.py. Out of the Workspace the hardwired link threw away a multi-pane layout
# on every visit.
#
# The origin travels as an explicit token, resolved HERE against a fixed table:
#
#   * not request.referrer — missing under some privacy settings, forgeable, and
#     a back link that depends on a request header is the kind of behaviour that
#     changes later for reasons nobody can reconstruct;
#   * not a free `next=` URL — that is an open redirect on a page behind SSO.
#
# An unknown or absent token falls back to the overview, which is where the link
# pointed before this existed.
_BACK_TARGETS = {
    'portal': ('dashboard.portal', 'Workspace'),
    'agents': ('dashboard.index', 'Agents'),
}


def _back_link(token: str | None) -> tuple[str, str]:
    endpoint, label = _BACK_TARGETS.get((token or '').strip().lower(), _BACK_TARGETS['agents'])
    return url_for(endpoint), label


@dashboard_bp.route('/instance/<instance_id>')
def instance_detail(instance_id):
    import uuid
    username = request.headers.get('X-Authentik-Username', 'anonymous')

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    # AGM-11 (#1039): match on BOTH slug candidates (current + legacy pre-hash,
    # #192), like api._owns_instance / proxy._owns already do. The exact-slug
    # compare 404'd a legacy-slug instance on its own settings page while
    # /api/tree happily linked to it. Candidates come from the CALLER's
    # username only, so cross-user isolation is unchanged.
    if not instance or instance['user_slug'] not in slug_candidates(username):
        return render_template('error.html', message='Instance not found.'), 404

    stats = None
    logs = ''
    if instance['state'] == 'running':
        stats = current_app.docker_client.get_container_stats(instance['container_name'])
        logs = current_app.docker_client.get_container_logs(instance['container_name'])

    # M030-S2: type_info needed by detail.html for the per-agent state-loss
    # warnings in the Danger Zone (different agents store different things).
    type_info = current_app.catalog.get_type(instance['agent_type'])

    # #36 / PR #84 — memory control on the settings page. Tier-gated (the
    # SERVER enforces it in provisioner.update_memory; the UI just reflects it).
    groups = [g.strip() for g in request.headers.get('X-Authentik-Groups', '').split('|') if g.strip()]
    from app.services.provisioner import (
        _tier_allows_custom_memory, MEM_PRESETS_GB, PIDS_PRESETS,
        resolve_pids_limit,
    )
    tier = current_app.db.resolve_user_tier(groups)
    can_set_memory = _tier_allows_custom_memory(tier)
    # Current chosen limit (persisted at launch/update), for the picker default.
    icfg = instance.get('config') or {}
    if isinstance(icfg, str):
        import json as _json
        try:
            icfg = _json.loads(icfg)
        except (ValueError, TypeError):
            icfg = {}
    current_mem = icfg.get('mem_limit') or (type_info or {}).get('mem_limit')

    # ga.2 (#219): Update/Reinstall affordance on the Settings page. Mirror the
    # card's "update available" detection (dashboard index): a newer version is
    # offered only when BOTH the running image_version and the catalog version
    # are known AND differ. When they match (or image_version is unknown), the
    # template falls back to a "Reinstall / Refresh" onto the current image —
    # which still matters for mutable `:latest` agents (coding-tools/paperclip)
    # whose tag never changes but whose digest can. Both go through /api/upgrade
    # (non-destructive; named volumes preserved).
    catalog_version = (type_info or {}).get('version')
    img_ver = instance.get('image_version')
    update_available = bool(img_ver and catalog_version and img_ver != catalog_version)
    update_target_version = catalog_version if update_available else None

    back_url, back_label = _back_link(request.args.get('from'))
    return render_template('dashboard/detail.html',
                           back_url=back_url,
                           back_label=back_label,
                           instance=instance,
                           type_info=type_info,
                           stats=stats,
                           logs=logs,
                           can_set_memory=can_set_memory,
                           mem_presets=list(MEM_PRESETS_GB),
                           mem_max_gb=current_app.provisioner._per_instance_max_gb(),
                           current_mem=current_mem,
                           update_available=update_available,
                           update_target_version=update_target_version,
                           catalog_version=catalog_version,
                           # #233 / #232 — per-instance name + PID cap.
                           display_name=instance_display_name(instance, type_info),
                           custom_name=icfg.get('custom_name') or '',
                           can_set_pids=can_set_memory,
                           pids_presets=list(PIDS_PRESETS),
                           current_pids=resolve_pids_limit(icfg, type_info))
