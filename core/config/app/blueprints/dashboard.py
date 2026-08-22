# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Dashboard blueprint — main landing page with health, alerts, resources, container matrix."""

import json
import os
import re
import subprocess
from flask import Blueprint, current_app, render_template

dashboard_bp = Blueprint('dashboard', __name__)


def _get_active_services(stack_root):
    """Return the set of compose services projected by the currently-active
    COMPOSE_PROFILES. `docker compose config --services` reads .env's profile
    set and lists exactly the services that should exist on this box —
    nothing more, nothing less. Inactive-profile services don't appear here,
    even if their containers still exist on disk in `Exited` state.
    Returns None if the projection failed (caller should not filter by
    profile in that case rather than blank the dashboard)."""
    try:
        result = subprocess.run(
            ['docker', 'compose', 'config', '--services'],
            capture_output=True, text=True, timeout=10,
            cwd=stack_root,
        )
        if result.returncode != 0:
            return None
        return {s for s in result.stdout.strip().splitlines() if s.strip()}
    except Exception:
        return None


def _get_container_list():
    """Return the set of persistent containers from active profiles. Drops:

    1. Cleanly-exited oneshots (init containers identified by the
       `razzfazz.init=true` compose label, with a `*-init` name fallback).
    2. Containers whose service is no longer in the active profile set
       (operator trimmed COMPOSE_PROFILES; their Exited containers should
       not drag the dashboard into 'degraded'). Detected via
       `docker compose config --services`.
    """
    stack_root = current_app.config['STACK_ROOT']
    try:
        result = subprocess.run(
            ['docker', 'compose', 'ps', '-a', '--format', 'json'],
            capture_output=True, text=True, timeout=10,
            cwd=stack_root,
        )
        containers = []
        for line in result.stdout.strip().splitlines():
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        active_services = _get_active_services(stack_root)

        out = []
        for c in containers:
            state = c.get('State', '')
            labels_raw = c.get('Labels', '') or ''
            # `docker compose ps --format json` emits Labels as a comma-
            # separated "k=v,k=v" string. Look for our marker.
            is_oneshot = 'razzfazz.init=true' in labels_raw
            # Fallback: anything named *-init, still-exited and unlabeled.
            if not is_oneshot and '-init' in c.get('Name', '') and state == 'exited':
                is_oneshot = True
            if is_oneshot and state == 'exited':
                continue  # don't count cleanly-exited oneshots against health

            # rc6.7 fix: drop containers whose service is no longer in the
            # active profile set. Operator trimmed COMPOSE_PROFILES and the
            # Exited containers from those profiles are not stack
            # degradation. Skip the filter only when active_services
            # projection failed (don't blank the dashboard on a transient
            # `docker compose config` failure).
            if active_services is not None:
                svc = c.get('Service', '')
                if svc and svc not in active_services:
                    continue

            out.append(c)
        return out
    except Exception:
        return []


def _get_disk_usage():
    """Get Docker disk usage. Queries the filesystem hosting the stack repo
    (which is also where /var/lib/docker volumes live on the default
    install). Falls back to `/` if STACK_ROOT is unset or not mounted in
    the container — both situations were observed when STACK_HOST_PATH
    drifted to empty (rc6.x snap→apt migration regression on prod)."""
    try:
        # STACK_ROOT mirrors STACK_HOST_PATH on the host; the razzfazz-config
        # bind mount uses the same path inside the container as out, so df
        # against this path measures the host filesystem from in here.
        target = os.environ.get('STACK_ROOT') or '/'
        if not os.path.exists(target):
            target = '/'
        result = subprocess.run(
            ['df', '-B1', target],
            capture_output=True, text=True, timeout=5,
        )
        # Join continuation lines (long device names wrap to next line)
        body = ' '.join(result.stdout.strip().splitlines()[1:])
        parts = body.split()
        # parts: [device, total, used, avail, pct%, mount]
        total = int(parts[1])
        used = int(parts[2])
        return {'total_gb': round(total / (1024**3), 1), 'used_gb': round(used / (1024**3), 1),
                'pct': round(used / total * 100, 1) if total else 0}
    except Exception:
        pass
    return {'total_gb': 0, 'used_gb': 0, 'pct': 0}


def _get_cpu_usage():
    """Get CPU usage from /proc/stat."""
    try:
        with open('/proc/stat') as f:
            line = f.readline()
        parts = line.split()
        idle = int(parts[4])
        total = sum(int(x) for x in parts[1:])
        # Read again after brief delay for delta
        import time
        time.sleep(0.1)
        with open('/proc/stat') as f:
            line2 = f.readline()
        parts2 = line2.split()
        idle2 = int(parts2[4])
        total2 = sum(int(x) for x in parts2[1:])
        delta_idle = idle2 - idle
        delta_total = total2 - total
        if delta_total > 0:
            return round((1 - delta_idle / delta_total) * 100, 1)
    except Exception:
        pass
    return 0


def _get_alerts(containers, system, config, backup_list):
    """Compute actionable alerts."""
    alerts = []

    # Container errors: restarting containers
    for c in containers:
        if c.get('State') == 'restarting':
            alerts.append({
                'level': 'error',
                'text': f'{c["Name"]} is restarting',
                'link': f'/modules/{c["Name"]}',
            })

    # Memory threshold
    if system.get('total_mb') and system.get('used_mb'):
        pct = system['used_mb'] / system['total_mb'] * 100
        if pct > 90:
            alerts.append({'level': 'error', 'text': f'Memory usage at {pct:.0f}%', 'link': '/modules/?status=running'})
        elif pct > 80:
            alerts.append({'level': 'warning', 'text': f'Memory usage at {pct:.0f}%', 'link': '/modules/?status=running'})

    # GPUStack API key
    gpustack_cfg = current_app.config_manager.get_gpustack_config()
    if not gpustack_cfg.get('api_key_set'):
        alerts.append({'level': 'warning', 'text': 'GPUStack API key not configured', 'link': '/settings/gpustack'})

    # Backup check
    if not backup_list:
        alerts.append({'level': 'warning', 'text': 'No backups found', 'link': '/backup/'})

    return alerts


def _get_backup_summary():
    """Get latest backup info.

    rc6.7 fix: accept both `.tar.gz` (legacy unencrypted) and `.tar.gz.gpg`
    (current, since rc2 F-A2 backup-encryption mitigation). Pre-rc6.7 the
    filter only matched `.tar.gz`, so a stack with the standard
    GPG-encrypted backup output silently showed "no backup available" on
    the dashboard, despite the backup container running its daily cron
    successfully.
    """
    backup_dir = os.path.join(current_app.config['STACK_ROOT'], 'backups')
    if not os.path.exists(backup_dir):
        return None
    files = sorted(
        [f for f in os.listdir(backup_dir)
         if f.endswith('.tar.gz') or f.endswith('.tar.gz.gpg')],
        reverse=True,
    )
    if not files:
        return None
    latest = files[0]
    path = os.path.join(backup_dir, latest)
    stat = os.stat(path)
    return {
        'filename': latest,
        'size_mb': round(stat.st_size / (1024**2), 1),
        'count': len(files),
        'encrypted': latest.endswith('.gpg'),
    }


@dashboard_bp.route('/')
def index():
    return render_template('dashboard/index.html', **_build_dashboard_context())


def _build_dashboard_context():
    """Shared data collection for index() + live_fragment(). Returns the
    full template context dict — both routes use it so the fragment renders
    identically regardless of whether it came from the initial page render
    or the 15-second HTMX poll."""
    containers = _get_container_list()
    running = sum(1 for c in containers if c.get('State') == 'running')
    total = len(containers)

    resources = current_app.resource_monitor.get_all()
    system = resources.get('system', {})
    profile_resources = resources.get('profiles', {})
    gpus = resources.get('gpus', [])
    cpu_pct = _get_cpu_usage()
    disk = _get_disk_usage()
    backup = _get_backup_summary()
    config = current_app.config_manager.get_all_config()
    alerts = _get_alerts(containers, system, config, [backup] if backup else [])

    # Recent audit log
    recent_entries = []
    log_path = current_app.config.get('AUDIT_LOG_PATH', '')
    if os.path.exists(log_path):
        try:
            with open(log_path) as f:
                lines = f.readlines()
            for line in reversed(lines[-5:]):
                line = line.strip()
                if line:
                    try:
                        recent_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    if total == 0:
        health_status = 'unknown'
    elif running == total:
        health_status = 'healthy'
    elif running > total * 0.5:
        health_status = 'degraded'
    else:
        health_status = 'critical'

    # Container matrix — Status text from `docker compose ps` carries the
    # uptime string ("Up 3 hours", "Restarting (1) 12 seconds ago", etc.)
    # which we surface as a sortable column.
    container_matrix = []
    for c in containers:
        name = c.get('Name', '')
        state = c.get('State', 'unknown')
        status_text = c.get('Status', '')
        image = c.get('Image', '')
        profile_id = '_untracked'
        for pid, res in profile_resources.items():
            if name in res.get('containers', {}):
                profile_id = pid
                break
        stats = profile_resources.get(profile_id, {}).get('containers', {}).get(name, {})
        # Numeric uptime in seconds for sorting; parsed loosely from status_text.
        uptime_s = _parse_uptime_seconds(status_text)
        container_matrix.append({
            'name': name,
            'profile': profile_id,
            'state': state,
            'status_text': status_text,
            'uptime_s': uptime_s,
            'image': image,
            'memory_mb': round(stats.get('memory_mb', 0), 1),
            'cpu_pct': round(stats.get('cpu_percent', 0), 1),
        })
    container_matrix.sort(key=lambda x: (0 if x['state'] != 'running' else 1, x['name']))

    manifest_updates = current_app.image_checker.get_manifest_updates()
    total_updates = sum(len(v) for v in manifest_updates.values())
    cve_alerts = current_app.image_checker.get_manifest_cve_alerts()
    manifest_info = current_app.image_checker.get_manifest_info()

    return dict(
        health_status=health_status,
        running=running,
        total=total,
        system=system,
        cpu_pct=cpu_pct,
        disk=disk,
        gpus=gpus,
        profile_resources=profile_resources,
        config=config,
        alerts=alerts,
        backup=backup,
        recent_entries=recent_entries,
        container_matrix=container_matrix,
        manifest_update_count=total_updates,
        cve_alerts=cve_alerts,
        manifest_info=manifest_info,
    )


def _parse_uptime_seconds(status_text):
    """Best-effort parse of 'Up X minutes/hours/days' into seconds for
    sortability. Returns 0 for non-running states ('Restarting', 'Exited',
    'Created', etc.) so they sort to the top of a descending uptime sort.
    """
    if not status_text or not status_text.startswith('Up '):
        return 0
    # 'Up 12 seconds', 'Up About a minute', 'Up 3 minutes', 'Up 2 hours',
    # 'Up 1 day', 'Up 3 days, 5 hours', 'Up 2 weeks', etc.
    m = re.search(r'Up (?:About (?:a|an) (?P<unit_only>\w+)|(?P<n>\d+)\s+(?P<unit>\w+))', status_text)
    if not m:
        return 1  # running but unparseable → keep above non-running 0
    unit_factors = {
        'second': 1, 'seconds': 1,
        'minute': 60, 'minutes': 60,
        'hour': 3600, 'hours': 3600,
        'day': 86400, 'days': 86400,
        'week': 604800, 'weeks': 604800,
        'month': 2592000, 'months': 2592000,
        'year': 31536000, 'years': 31536000,
    }
    if m.group('unit_only'):
        return unit_factors.get(m.group('unit_only'), 1)
    n = int(m.group('n'))
    unit = m.group('unit')
    return n * unit_factors.get(unit, 1)


@dashboard_bp.route('/dashboard/live-fragment')
def live_fragment():
    """Partial fragment for the dashboard's live data. Polled via HTMX
    every ~15 s — keeps the page fresh without a full reload (modals,
    scroll position, form state preserved). The same template renders
    on first page load via {% include %} from index.html."""
    return render_template('dashboard/_live_fragment.html', **_build_dashboard_context())
