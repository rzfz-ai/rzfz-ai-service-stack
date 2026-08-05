# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Modules blueprint — module overview and detail pages."""

from flask import Blueprint, abort, current_app, render_template, request

modules_bp = Blueprint('modules', __name__, url_prefix='/modules')


@modules_bp.route('/')
def overview():
    profiles = current_app.profile_manager.get_all_profiles()
    enabled = current_app.profile_manager.get_enabled_profiles()
    profile_resources = current_app.resource_monitor.get_profile_resources()

    # Get filter params
    category = request.args.get('category', '')
    status = request.args.get('status', '')
    maturity = request.args.get('maturity', '')
    search = request.args.get('q', '')

    # Manifest-based update detection (M013)
    manifest_updates = current_app.image_checker.get_manifest_updates()

    module_list = []
    for pid, profile in profiles.items():
        is_enabled = pid in enabled or pid == 'core'
        res = profile_resources.get(pid, {})
        actual_mb = res.get('total_mb', 0)
        updates = manifest_updates.get(pid, [])
        entry = {
            'id': pid,
            'enabled': is_enabled,
            'health': current_app.profile_manager.get_profile_health(pid) if is_enabled else 'gray',
            'actual_memory_mb': round(actual_mb, 1) if actual_mb else None,
            'has_updates': len(updates) > 0,
            'update_count': len(updates),
            **profile,
        }
        if category and profile.get('category') != category:
            continue
        if status == 'enabled' and not is_enabled:
            continue
        if status == 'disabled' and is_enabled:
            continue
        if status == 'running' and not actual_mb:
            continue
        if maturity and profile.get('maturity') != maturity:
            continue
        if search and search.lower() not in profile.get('name', '').lower():
            continue
        if status == 'updates' and not entry['has_updates']:
            continue
        module_list.append(entry)

    # Sort
    sort_by = request.args.get('sort', '')
    if sort_by == 'name':
        module_list.sort(key=lambda m: m.get('name', '').lower())
    elif sort_by == 'memory':
        module_list.sort(key=lambda m: m.get('actual_memory_mb') or m.get('ram_estimate_mb', 0), reverse=True)
    elif sort_by == 'category':
        module_list.sort(key=lambda m: m.get('category', ''))
    elif sort_by == 'enabled':
        module_list.sort(key=lambda m: (0 if m['enabled'] else 1, m.get('name', '').lower()))

    categories = sorted(set(p.get('category', 'Other') for p in profiles.values()))
    # rc6.7 #5: derive maturity options from the live profiles.yaml values
    # instead of hardcoding. The previous template hardcoded
    # production/stable/experimental — `stable` matched zero modules
    # (profiles.yaml uses production/experimental/deprecated) and
    # `deprecated` was missing entirely. Sorted for stable rendering;
    # the template title-cases the labels.
    maturities = sorted(
        {p.get('maturity') for p in profiles.values() if p.get('maturity')}
    )

    return render_template('modules/overview.html',
                           modules=module_list,
                           categories=categories,
                           maturities=maturities,
                           filter_category=category,
                           filter_status=status,
                           filter_maturity=maturity,
                           filter_sort=sort_by,
                           search_query=search)


@modules_bp.route('/llm-runtime')
def llm_runtime():
    """M029-S04 — LLM runtime stable/experimental toggle panel."""
    state = current_app.apply_manager.get_llm_runtime_state()
    return render_template('modules/llm-runtime.html', state=state)


@modules_bp.route('/<profile_id>')
def detail(profile_id):
    profile = current_app.profile_manager.get_profile(profile_id)
    if not profile:
        abort(404)

    enabled = current_app.profile_manager.get_enabled_profiles()
    is_enabled = profile_id in enabled or profile_id == 'core'

    # Get live container data — only for enabled profiles
    # (shared containers like gpustack appear in multiple profiles but belong to the enabled one)
    containers_display = []
    actual_total_mb = 0
    if is_enabled:
        profile_resources = current_app.resource_monitor.get_profile_resources()
        res = profile_resources.get(profile_id, {})
        container_stats = res.get('containers', {})
        actual_total_mb = res.get('total_mb', 0)
        container_status = current_app.profile_manager.get_container_status(profile_id)

        for c in profile.get('containers', []):
            name = c['name']
            status_info = container_status.get(name, {})
            stats_info = container_stats.get(name, {})
            containers_display.append({
                'name': name,
                'image': c.get('image', ''),
                'status': status_info.get('status', 'unknown'),
                'health': status_info.get('health', 'none'),
                'memory_mb': round(stats_info.get('memory_mb', 0), 1),
                'cpu_percent': round(stats_info.get('cpu_percent', 0), 2),
            })
    else:
        for c in profile.get('containers', []):
            containers_display.append({
                'name': c['name'],
                'image': c.get('image', ''),
                'status': 'stopped',
                'health': 'none',
                'memory_mb': 0,
                'cpu_percent': 0,
            })

    health = current_app.profile_manager.get_profile_health(profile_id) if is_enabled else 'gray'

    # Manifest-based update info
    manifest_updates = current_app.image_checker.get_manifest_updates()
    profile_updates = manifest_updates.get(profile_id, [])

    return render_template('modules/detail.html',
                           profile_id=profile_id,
                           profile=profile,
                           is_enabled=is_enabled,
                           health=health,
                           containers=containers_display,
                           actual_total_mb=round(actual_total_mb, 1),
                           is_core=profile_id == 'core',
                           updates=profile_updates)
