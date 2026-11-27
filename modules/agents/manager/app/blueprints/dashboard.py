# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""User-facing dashboard — agent instance cards and management UI."""

import os
import socket
import threading
import time

from app.services import agent_updates
from app.services.catalog import launchable_presets
from urllib.parse import urlparse

from flask import Blueprint, current_app, make_response, render_template, request, url_for

from app.services.ingress import ingress_ok, log_refusal
from app.services.names import suggest_name
from app.services import ownership
from app.services.provisioner import instance_display_name, slug_candidates

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
    groups = [g.strip() for g in request.headers.get('X-Authentik-Groups', '').split('|') if g.strip()]
    main_domain = os.environ.get('MAIN_DOMAIN', 'localhost')
    chat_base = f"https://chat.{main_domain}"

    agent_types = current_app.catalog.get_types()
    # #2518: read the user's instances under EVERY slug they own (current +
    # legacy pre-hash, #192) — the same `slug_candidates` read `/api/tree` and
    # every ownership check use. The single-slug read showed a running
    # legacy-slug instance as "Not launched". The footer count below is
    # `len(instances)`, so cards and footer share this one read.
    instances = current_app.db.get_user_instances(
        slug_candidates(username), user_id=ownership.authenticated_uid())
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

    # #2517 §3: the spawn modal prefills a name the user does not use yet.
    taken = [instance_display_name(i) for i in instances]
    # Fix round 1: one suggestion per type, INCLUDING the types the user
    # already runs — a quick-start tile or a scoped open of such a type has no
    # `.type-opt` tile to read it from, and its display name is taken.
    suggested_names = {at['id']: suggest_name(at.get('display_name') or at.get('id') or '', taken)
                       for at in agent_types}

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

          # M030-S2 + #3187: "Update available" detection — ONE reconciled
          # signal: version drift OR a proven image id drift of the running
          # container (companion included) vs the catalog image. The image
          # compare catches a rebuilt mutable tag after `rzfz upgrade` (same
          # version string, new image id) — the case the version-only check
          # could not see. Pre-S2 rows (NULL image_version) with unreadable
          # docker images still show no badge: "cannot tell" is not "behind".
          update_available = False
          update_target_version = None
          if instance:
              update_available = agent_updates.update_available_for(
                  current_app.provisioner, instance, at)
              if update_available and at.get('version'):
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
              'suggested_name': suggest_name(at.get('display_name') or at.get('id') or '', taken),
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

    # #2554: the launch form's identity choice (catalogue admins, coding agents
    # only); the launch route re-checks it (api._authorize_identity).
    from razzfazz_common.rbac_guard import allows
    from app.services.provisioner import SANDBOXED_TYPES
    can_launch_technical = allows('agent-manager', 'admin', groups)
    sandboxed_types = sorted(SANDBOXED_TYPES)

    return dict(
        cards=cards, tier=tier,
        can_launch_technical=can_launch_technical, sandboxed_types=sandboxed_types,
        total_instances=total_instances, presets=presets,
        username=username,
        auto_launch=auto_launch, auto_start=auto_start,
        suggested_names=suggested_names,
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


def launch_intent(args, catalog):
    """#920 — a Stack-App launch intent from a portal tile
    (``/dashboard?launch=<type>&repo=&cmd=[&resume=]``).

    Only an ENABLED catalog type counts; anything else yields ``None`` and the
    Workspace opens as usual. The values are returned as strings for the
    template to render escaped (``tojson``). A GET never launches anything:
    the page pre-fills, the user confirms."""
    at = (args.get('launch') or '').strip()
    if not at:
        return None
    t = catalog.get_type(at)
    if not t or not t.get('enabled', True):
        return None
    return {'agent_type': at, 'repo': (args.get('repo') or '')[:200],
            'cmd': (args.get('cmd') or '')[:500], 'resume': (args.get('resume') or '')[:100]}


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
                           mcp_active=mcp_active, mcp_url=mcp_url,
                           launch_intent=launch_intent(request.args, current_app.catalog))


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


def _settings_node(instance, type_info, update_target_version=None) -> dict:
    """The portal-node subset the settings surfaces hand to AmSettings.wire
    (2026.09-ga.4, #2517 §5), built with the helpers `/api/tree` uses so the
    full page and the pane run the action registry on the same shape."""
    from app.blueprints.api import _TREE_STATUS, _type_mounts
    from app.services.catalog import ui_kind_for
    at = type_info or {}
    running = instance['state'] == 'running'
    ports = at.get('ports') or {}
    if isinstance(ports, str):
        import json as _json
        try:
            ports = _json.loads(ports) or {}
        except (ValueError, TypeError):
            ports = {}
    kind = ui_kind_for(at) if at else 'terminal'
    web = bool(isinstance(ports, dict) and ports.get('internal')) and kind in ('web', 'both')
    try:
        files = bool(_type_mounts(at)) if at else False
    except Exception:  # noqa: BLE001 - advisory: the Files tab is not on this page
        files = False
    return {
        'instance_id': str(instance['id']),
        'label': instance_display_name(instance, type_info),
        'status': _TREE_STATUS.get(instance['state'], 'stopped'),
        'has_web_ui': web,
        'ui': {'terminal': running, 'web': running and web},
        'files': files,
        'settings_url': f"/instance/{instance['id']}?from=portal",
        # the Update button's confirmation names the version (fix round 1)
        'update_target_version': update_target_version,
    }


def _instance_settings_context(instance_id, username):
    """Everything the settings partial (`dashboard/_instance_settings.html`)
    renders, or None when the instance does not exist or is not the caller's.

    Shared by the full settings page and the pane fragment (2026.09-ga.4,
    #2517 §5), so the two cannot disagree on WHAT they show or on WHO may see
    it. AGM-11 (#1039): ownership matches BOTH slug candidates (current +
    legacy pre-hash, #192), like api._owns_instance / proxy._owns. Candidates
    come from the CALLER's username only, so cross-user isolation holds.
    """
    import uuid
    try:
        iid = uuid.UUID(str(instance_id))
    except (ValueError, TypeError, AttributeError):
        return None
    instance = current_app.db.get_instance(iid)
    if not instance or not ownership.owns(instance, username):
        return None
    # Delete is a SOFT delete: get_instance still returns the row, now in
    # state 'destroyed'. Its former owner must not get its settings back
    # (2026.09-ga.4 U6 fix round 1).
    if instance.get('state') == 'destroyed':
        return None

    stats = None
    logs = ''
    if instance['state'] == 'running':
        stats = current_app.docker_client.get_container_stats(instance['container_name'])
        logs = current_app.docker_client.get_container_logs(instance['container_name'])

    # M030-S2: type_info needed for the per-agent state-loss warnings in the
    # Danger Zone (different agents store different things).
    type_info = current_app.catalog.get_type(instance['agent_type'])

    # #36 / PR #84 — memory control. Tier-gated (the SERVER enforces it in
    # provisioner.update_memory; the UI just reflects it).
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

    # ga.2 (#219) + #3187: Update/Reinstall affordance. #3187 reconciles the
    # detection: version drift OR a proven image id drift of the running
    # container(s) vs the catalog image (a rebuilt `:latest` after an upgrade
    # has the same version string but a new image id). Otherwise
    # "Reinstall / Refresh" onto the current image. Both go through
    # /api/upgrade (non-destructive; named volumes preserved).
    catalog_version = (type_info or {}).get('version')
    update_available = agent_updates.update_available_for(
        current_app.provisioner, instance, type_info)
    update_target_version = catalog_version if update_available else None

    return dict(
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
        current_pids=resolve_pids_limit(icfg, type_info),
        node=_settings_node(instance, type_info, update_target_version),
    )


def _private(body, status=200):
    """A per-user settings answer: never stored by a shared cache or the
    browser's back/forward cache of another session (fix round 1)."""
    resp = make_response(body, status)
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp


@dashboard_bp.route('/instance/<instance_id>')
def instance_detail(instance_id):
    username = request.headers.get('X-Authentik-Username', 'anonymous')
    ctx = _instance_settings_context(instance_id, username)
    if ctx is None:
        return _private(render_template('error.html', message='Instance not found.'), 404)
    back_url, back_label = _back_link(request.args.get('from'))
    return _private(render_template('dashboard/detail.html',
                                    back_url=back_url, back_label=back_label, **ctx))


@dashboard_bp.route('/instance/<instance_id>/fragment')
def instance_settings_fragment(instance_id):
    """The settings partial alone, for the portal's Settings tab (2026.09-ga.4,
    #2517 §5). Same owner scope and same context as `/instance/<id>`; no page
    chrome, no script. A foreign or unknown id is an empty 404 (the pane shows
    "Could not load settings")."""
    username = request.headers.get('X-Authentik-Username', 'anonymous')
    ctx = _instance_settings_context(instance_id, username)
    if ctx is None:
        return _private('', 404)
    return _private(render_template('dashboard/_instance_settings.html', embedded=True, **ctx))
