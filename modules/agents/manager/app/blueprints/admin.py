# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Admin views — all instances, quota management, system overview, and admin API."""

import json

from flask import Blueprint, current_app, jsonify, render_template, request

from razzfazz_common.auth import parse_authentik_headers

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# Internal service accounts (e.g., Config UI) that bypass Authentik group checks.
INTERNAL_SERVICE_ACCOUNTS = {'config-ui-admin'}

# Groups granting admin access. MUST stay in sync with the `is_admin` context
# processor in app/__init__.py — otherwise the dashboard shows the "Admin" link
# (is_admin true) but this route 403s (M033 A4 updated is_admin to accept both
# groups but left this check on the old single-group substring → a Super Admin
# saw the link yet got "Admin access required" on click).
ADMIN_GROUPS = ('authentik Admins', 'razzfazz.ai Super Admins')


def _require_admin():
    # Parse via the shared lib (pipe-delimited X-Authentik-Groups → exact tokens),
    # matching the dashboard's is_admin semantics.
    info = parse_authentik_headers()
    # Allow internal service-to-service calls from Config UI.
    if info['username'] in INTERNAL_SERVICE_ACCOUNTS:
        return True
    return any(g in ADMIN_GROUPS for g in info['groups'])


@admin_bp.before_request
def check_admin():
    if request.path.startswith('/admin/') and not _require_admin():
        if request.path.startswith('/admin/api/'):
            return jsonify({'error': 'Admin access required'}), 403
        return render_template('error.html', message='Admin access required.'), 403


@admin_bp.route('/')
def index():
    instances = current_app.db.get_all_instances()
    agent_types = current_app.catalog.get_types(enabled_only=False)
    tiers = current_app.db.get_quota_tiers()
    total = current_app.db.count_all_instances()
    max_instances = current_app.config['AGENT_MAX_INSTANCES']

    return render_template('admin/index.html',
                           instances=instances,
                           agent_types=agent_types,
                           tiers=tiers,
                           total=total,
                           max_instances=max_instances)


@admin_bp.route('/audit')
def audit_log():
    entries = current_app.db.get_audit_log(limit=200)
    return render_template('admin/audit.html', entries=entries)


@admin_bp.route('/api/force-stop/<instance_id>', methods=['POST'])
def force_stop(instance_id):
    if not _require_admin():
        return jsonify({'error': 'Forbidden'}), 403

    username = request.headers.get('X-Authentik-Username', 'admin')
    iid, message = current_app.provisioner.stop(instance_id, username)
    current_app.db.log_audit(username, 'admin_force_stop', instance_id=iid)
    return jsonify({'message': message})


@admin_bp.route('/api/force-delete/<instance_id>', methods=['POST'])
def force_delete(instance_id):
    if not _require_admin():
        return jsonify({'error': 'Forbidden'}), 403

    username = request.headers.get('X-Authentik-Username', 'admin')
    iid, message = current_app.provisioner.delete(instance_id, username)
    current_app.db.log_audit(username, 'admin_force_delete', instance_id=iid)
    return jsonify({'message': message})


# ---------------------------------------------------------------------------
# Admin API — Agent Types (consumed by Config UI)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/types')
def api_get_types():
    """Return all agent types (including disabled)."""
    rows = current_app.catalog.get_types(enabled_only=False)
    result = []
    for row in rows:
        result.append({
            'id': row['id'],
            'display_name': row['display_name'],
            'tier': row['tier'],
            'image': row['image'],
            'version': row['version'],
            'mem_limit': row['mem_limit'],
            'cpu_limit': row['cpu_limit'],
            'idle_timeout': row['idle_timeout'],
            'enabled': row['enabled'],
            'description': row['description'],
            'requires_docker_socket': row['requires_docker_socket'],
            'requires_db': row['requires_db'],
        })
    return jsonify(result)


@admin_bp.route('/api/types/<type_id>', methods=['PUT'])
def api_update_type(type_id):
    """Update mutable fields of an agent type."""
    existing = current_app.db.get_agent_type(type_id)
    if not existing:
        return jsonify({'error': f'Unknown agent type: {type_id}'}), 404

    data = request.get_json(silent=True) or {}

    # Build update dict — only allow mutable fields
    update = dict(existing)
    if 'enabled' in data:
        update['enabled'] = bool(data['enabled'])
    if 'mem_limit' in data:
        update['mem_limit'] = str(data['mem_limit'])
    if 'idle_timeout' in data:
        update['idle_timeout'] = int(data['idle_timeout'])
    if 'cpu_limit' in data:
        update['cpu_limit'] = float(data['cpu_limit'])

    # Convert JSONB fields back to JSON strings for the upsert
    for field in ('ports', 'volumes', 'env_template'):
        val = update.get(field)
        if val is not None and not isinstance(val, str):
            update[field] = json.dumps(val)

    current_app.db.upsert_agent_type(update)

    username = request.headers.get('X-Authentik-Username', 'admin')
    current_app.db.log_audit(username, 'admin_update_type', agent_type=type_id,
                             details={'mem_limit': update['mem_limit'],
                                      'idle_timeout': update['idle_timeout'],
                                      'enabled': update['enabled']})

    return jsonify({'ok': True, 'type_id': type_id})


# ---------------------------------------------------------------------------
# Admin API — Quota Tiers (consumed by Config UI)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/tiers')
def api_get_tiers():
    """Return all quota tiers."""
    rows = current_app.db.get_quota_tiers()
    result = []
    for row in rows:
        result.append({
            'id': row['id'],
            'display_name': row['display_name'],
            'max_per_type': row['max_per_type'],
            'max_heavy': row['max_heavy'],
            'allowed_types': row['allowed_types'],
            'priority': row['priority'],
        })
    return jsonify(result)


@admin_bp.route('/api/tiers/<tier_id>', methods=['PUT'])
def api_update_tier(tier_id):
    """Update a quota tier."""
    existing = current_app.db.get_quota_tier(tier_id)
    if not existing:
        return jsonify({'error': f'Unknown quota tier: {tier_id}'}), 404

    data = request.get_json(silent=True) or {}

    update = {
        'id': tier_id,
        'display_name': data.get('display_name', existing['display_name']),
        'max_per_type': int(data.get('max_per_type', existing['max_per_type'])),
        'max_heavy': int(data.get('max_heavy', existing['max_heavy'])),
        'priority': int(data.get('priority', existing['priority'])),
    }

    # allowed_types: None means all types, list means restricted
    if 'allowed_types' in data:
        at = data['allowed_types']
        if at is None or at == []:
            update['allowed_types'] = None
        else:
            update['allowed_types'] = json.dumps(at)
    else:
        val = existing['allowed_types']
        update['allowed_types'] = json.dumps(val) if val is not None else None

    current_app.db.upsert_quota_tier(update)

    username = request.headers.get('X-Authentik-Username', 'admin')
    current_app.db.log_audit(username, 'admin_update_tier',
                             details={'tier_id': tier_id,
                                      'max_per_type': update['max_per_type'],
                                      'max_heavy': update['max_heavy']})

    return jsonify({'ok': True, 'tier_id': tier_id})


# ---------------------------------------------------------------------------
# Admin API — Global Config (consumed by Config UI)
# ---------------------------------------------------------------------------

@admin_bp.route('/api/config')
def api_get_config():
    """Return current runtime config (env-based settings + DB-backed memory
    governance). #36 / PR #84 — the memory budget + per-user cap are DB-backed
    (agent_settings) so a Config-UI change is LIVE and dodges the .env inode
    bug. Also surfaces the DYNAMIC host-derived budget ceiling (host RAM minus
    the core-stack reserve, computed live) so the Config UI can bound the input
    per-box rather than with a hardcoded cap.
    """
    def _int(v, default=None):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    host_max = 0
    try:
        # host RAM - reserve, computed live by the provisioner.
        host_max = int(current_app.provisioner.effective_mem_budget_mb(
            configured_mb=10**12))  # huge → returns the pure host ceiling
    except Exception:  # noqa: BLE001
        host_max = 0

    budget = _int(current_app.db.get_agent_setting('global_mem_budget_mb'), None)
    per_user = _int(current_app.db.get_agent_setting('per_user_mem_mb'), None)

    running_mb = 0
    try:
        running_mb = int(current_app.db.sum_running_mem_mb())
    except Exception:  # noqa: BLE001
        running_mb = 0

    return jsonify({
        'max_instances': current_app.config['AGENT_MAX_INSTANCES'],
        'idle_timeout_lightweight': current_app.config['AGENT_IDLE_TIMEOUT_LIGHTWEIGHT'],
        'idle_timeout_heavy': current_app.config['AGENT_IDLE_TIMEOUT_HEAVY'],
        'cleanup_after_days': current_app.config['AGENT_CLEANUP_AFTER_DAYS'],
        'agents_domain': current_app.config['AGENTS_DOMAIN'],
        'total_instances': current_app.db.count_all_instances(),
        # Memory governance (#36 / PR #84).
        'global_mem_budget_mb': budget if budget is not None else host_max,
        'per_user_mem_mb': per_user,
        # Dynamic per-box ceiling for the Config UI's budget input.
        'mem_budget_host_max_mb': host_max,
        'per_instance_max_gb': current_app.config.get('AGENT_MEM_PER_INSTANCE_MAX_GB', 16),
        'mem_in_use_mb': running_mb,
    })


@admin_bp.route('/api/config', methods=['PUT'])
def api_update_config():
    """Persist the DB-backed memory-governance settings from the Config UI.

    #36 / PR #84. Body may carry `global_mem_budget_mb` and/or `per_user_mem_mb`
    (MB integers). The global budget is CLAMPED to the dynamic host ceiling
    (host RAM minus the core-stack reserve) so an admin can't set a budget the
    box can't back. Written to agent_settings (live; no agent-manager recreate;
    no .env inode-bug exposure).
    """
    if not _require_admin():
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    written = {}

    # Host ceiling for clamping (live).
    host_max = 0
    try:
        host_max = int(current_app.provisioner.effective_mem_budget_mb(configured_mb=10**12))
    except Exception:  # noqa: BLE001
        host_max = 0

    if 'global_mem_budget_mb' in data:
        try:
            val = int(data['global_mem_budget_mb'])
        except (TypeError, ValueError):
            return jsonify({'error': 'global_mem_budget_mb must be an integer (MB)'}), 400
        val = max(0, val)
        if host_max:
            val = min(val, host_max)  # can't exceed what the box can back
        current_app.db.set_agent_setting('global_mem_budget_mb', val)
        written['global_mem_budget_mb'] = val

    if 'per_user_mem_mb' in data:
        try:
            val = int(data['per_user_mem_mb'])
        except (TypeError, ValueError):
            return jsonify({'error': 'per_user_mem_mb must be an integer (MB)'}), 400
        val = max(0, val)
        current_app.db.set_agent_setting('per_user_mem_mb', val)
        written['per_user_mem_mb'] = val

    username = request.headers.get('X-Authentik-Username', 'admin')
    current_app.db.log_audit(username, 'admin_update_mem_config',
                             details=written)
    return jsonify({'ok': True, 'written': written})
