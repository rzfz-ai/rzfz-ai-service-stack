# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""User-facing dashboard — agent instance cards and management UI."""

import os

from flask import Blueprint, current_app, render_template, request

from app.services.provisioner import make_user_slug

dashboard_bp = Blueprint('dashboard', __name__)

# M020 S08 — agent_type → OpenWebUI pipe model id (for "Open in Chat" links).
# Maps to the manifold pipes shipped in M020 S03/S04/S05.
_CHAT_MODEL_FOR_TYPE = {
    'hermes':       'hermes.personal',
    'moltis':       'moltis.personal',
    'coding-tools': 'opencode.personal',
}

def _chat_model_for_type(agent_type: str) -> str | None:
    return _CHAT_MODEL_FOR_TYPE.get(agent_type)


def _build_dashboard_context():
    username = request.headers.get('X-Authentik-Username', 'anonymous')
    user_slug = make_user_slug(username)
    groups = [g.strip() for g in request.headers.get('X-Authentik-Groups', '').split('|') if g.strip()]
    main_domain = os.environ.get('MAIN_DOMAIN', 'localhost')
    chat_base = f"https://chat.{main_domain}"

    agent_types = current_app.catalog.get_types()
    instances = current_app.db.get_user_instances(user_slug)
    instance_map = {i['agent_type']: i for i in instances}
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

    cards = []
    for at in agent_types:
        instance = instance_map.get(at['id'])
        allowed_types = tier.get('allowed_types') if tier else []
        type_allowed = (tier and (allowed_types is None or at['id'] in allowed_types))
        heavy_ok = (at['tier'] != 'heavy' or (tier and tier['max_heavy'] > 0))

        stats = None
        if instance and instance['state'] == 'running':
            stats = current_app.docker_client.get_container_stats(instance['container_name'])

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

        cards.append({
            'type': at,
            'instance': instance,
            'stats': stats,
            'can_launch': type_allowed and heavy_ok and not instance,
            'restricted': not type_allowed or not heavy_ok,
            'chat_url': chat_url,
            'open_url': open_url,
            'update_available': update_available,
            'update_target_version': update_target_version,
            'mem_limit': mem_limit_str,
        })

    total_instances = len(instances)
    total_mem = sum(
        (s['stats']['mem_usage_mb'] if s['stats'] else 0)
        for s in cards if s['instance'] and s['instance']['state'] == 'running'
    )

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
    mcp_active = bool(os.environ.get('MCP_MANAGER_URL', '').strip())
    mcp_url = f"https://mcp.{main_domain}/dashboard"

    return dict(
        cards=cards, tier=tier,
        total_instances=total_instances, total_mem=total_mem,
        username=username,
        auto_launch=auto_launch, auto_start=auto_start,
        mcp_active=mcp_active, mcp_url=mcp_url,
        # #36 / PR #84 — memory picker context (tier-gated).
        can_set_memory=can_set_memory, mem_presets=mem_presets,
        mem_default_gb=mem_default_gb, mem_max_gb=mem_max_gb,
    )


@dashboard_bp.route('/')
@dashboard_bp.route('/dashboard')   # M020 S08 — Authentik launch URL points here
def index():
    return render_template('dashboard/index.html', **_build_dashboard_context())


@dashboard_bp.route('/instance/<instance_id>')
def instance_detail(instance_id):
    import uuid
    username = request.headers.get('X-Authentik-Username', 'anonymous')
    user_slug = make_user_slug(username)

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not instance or instance['user_slug'] != user_slug:
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
    from app.services.provisioner import _tier_allows_custom_memory, MEM_PRESETS_GB
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

    return render_template('dashboard/detail.html',
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
                           catalog_version=catalog_version)
