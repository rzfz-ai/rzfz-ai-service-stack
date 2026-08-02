# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Settings blueprint — Domain/TLS, Auth, SMTP, GPUStack, Secrets, Personal Agents configuration pages."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

import requests as http_requests
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from app.services import gpustack_client, model_sideload
from app.services.model_sideload import SideloadError

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')

CERTS_DIR = '/stack/certs'


def _certs_dir():
    """Resolve the TLS certs directory. Honors a patched module-level
    CERTS_DIR (tests) but otherwise derives from STACK_ROOT so it tracks
    the live mount (../certs:/stack/certs)."""
    if CERTS_DIR != '/stack/certs':
        return CERTS_DIR
    return os.path.join(current_app.config.get('STACK_ROOT', '/stack'), 'certs')


# ---------------------------------------------------------------------------
# Domain & TLS
# ---------------------------------------------------------------------------

@settings_bp.route('/domain')
def domain():
    cfg = current_app.config_manager.get_domain_tls_config()
    cfg['cert_status'] = _get_cert_status()
    return render_template('settings/domain.html', config=cfg)


@settings_bp.route('/domain', methods=['POST'])
def domain_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    changes = []

    new_domain = request.form.get('domain', '').strip()
    if new_domain:
        current_app.config_manager.update_domain(new_domain)
        changes.append(f'domain={new_domain}')

    data = {
        'tls_mode': request.form.get('tls_mode', 'letsencrypt'),
        'letsencrypt_email': request.form.get('letsencrypt_email', ''),
    }
    current_app.config_manager.update_domain_tls(data)
    changes.append(f'tls={data["tls_mode"]}')

    if data['tls_mode'] == 'certificate':
        cert_file = request.files.get('cert_file')
        key_file = request.files.get('key_file')
        if cert_file and cert_file.filename and key_file and key_file.filename:
            # #22: PEM sanity-check + restrictive perms, ported from the
            # retired razzfazz-setup /api/upload-certificate handler. The
            # private key must never be world-readable.
            cert_bytes = cert_file.read()
            key_bytes = key_file.read()
            try:
                cert_text = cert_bytes.decode('utf-8')
                key_text = key_bytes.decode('utf-8')
            except UnicodeDecodeError:
                flash('Certificate or key file is not a valid text/PEM file.', 'error')
                return redirect(url_for('settings.domain'))
            if '-----BEGIN CERTIFICATE-----' not in cert_text:
                flash('Certificate file is not a valid PEM certificate.', 'error')
                return redirect(url_for('settings.domain'))
            if '-----BEGIN' not in key_text or 'PRIVATE KEY' not in key_text:
                flash('Key file is not a valid PEM private key.', 'error')
                return redirect(url_for('settings.domain'))
            certs_dir = _certs_dir()
            os.makedirs(certs_dir, exist_ok=True)
            cert_path = os.path.join(certs_dir, 'cert.pem')
            key_path = os.path.join(certs_dir, 'key.pem')
            with open(cert_path, 'wb') as f:
                f.write(cert_bytes)
            os.chmod(cert_path, 0o644)
            with open(key_path, 'wb') as f:
                f.write(key_bytes)
            os.chmod(key_path, 0o600)
            changes.append('cert_uploaded')

    risk = 'danger' if new_domain else 'caution'
    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='domain_tls',
        detail=f'Updated domain/TLS: {", ".join(changes)}',
        risk=risk, outcome='success',
    )

    # Restart Caddy to apply TLS/domain changes
    action_id, err = current_app.apply_manager.apply_service_restart(
        ['caddy'], f'Updated {", ".join(changes)}', user, source_ip, risk=risk)
    if err:
        flash(f'Settings saved but restart failed: {err}', 'error')
    else:
        flash('Domain & TLS settings saved. Caddy restarting...', 'success')
    return redirect(url_for('settings.domain'))


def _get_cert_status():
    cert_path = os.path.join(_certs_dir(), 'cert.pem')
    if not os.path.exists(cert_path):
        return None
    try:
        result = subprocess.run(
            ['openssl', 'x509', '-in', cert_path, '-noout',
             '-subject', '-issuer', '-enddate', '-dates'],
            capture_output=True, text=True, timeout=5,
        )
        info = {}
        for line in result.stdout.strip().splitlines():
            if line.startswith('issuer='):
                info['issuer'] = line.split('=', 1)[1].strip()
            elif line.startswith('notAfter='):
                info['expiry'] = line.split('=', 1)[1].strip()
        try:
            from datetime import datetime
            expiry_dt = datetime.strptime(info.get('expiry', ''), '%b %d %H:%M:%S %Y %Z')
            info['days_remaining'] = (expiry_dt - datetime.utcnow()).days
        except Exception:
            info['days_remaining'] = None
        return info
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

@settings_bp.route('/auth')
def auth():
    cfg = current_app.config_manager.get_auth_config()
    return render_template('settings/auth.html', config=cfg)


@settings_bp.route('/auth', methods=['POST'])
def auth_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    data = {}
    for key in ('ENABLE_GOOGLE_OAUTH', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET',
                'ENABLE_ENTRA_OAUTH', 'ENTRA_CLIENT_ID', 'ENTRA_CLIENT_SECRET',
                'ENTRA_TENANT_ID', 'ENTRA_OAUTH_DOMAIN',
                'ENABLE_OPENWEBUI_OIDC', 'OPENWEBUI_OIDC_CLIENT_ID', 'OPENWEBUI_OIDC_CLIENT_SECRET',
                'ENABLE_GITEA_AUTHENTIK_OIDC', 'GITEA_OIDC_CLIENT_ID', 'GITEA_OIDC_CLIENT_SECRET'):
        val = request.form.get(key, '')
        if key.startswith('ENABLE_'):
            data[key] = 'true' if val in ('true', 'on', '1') else 'false'
        elif val.strip():  # Only update non-empty values (preserve existing secrets)
            data[key] = val.strip()

    current_app.config_manager.update_auth(data)

    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='auth',
        detail='Updated authentication settings',
        risk='caution', outcome='success',
    )

    # Restart Authentik + affected services
    action_id, err = current_app.apply_manager.apply_service_restart(
        ['authentik-server', 'authentik-worker'],
        'Updated authentication settings', user, source_ip, risk='caution')
    flash('Authentication settings saved. Authentik restarting...', 'success')
    return redirect(url_for('settings.auth'))


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

@settings_bp.route('/smtp')
def smtp():
    cfg = current_app.config_manager.get_smtp_config()
    return render_template('settings/smtp.html', config=cfg)


@settings_bp.route('/smtp', methods=['POST'])
def smtp_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    data = {
        'mode': request.form.get('mode', 'relay'),
        'from': request.form.get('from', ''),
        'relay_host': request.form.get('relay_host', ''),
        'relay_port': request.form.get('relay_port', '587'),
        'relay_username': request.form.get('relay_username', ''),
        'relay_password': request.form.get('relay_password', ''),
    }
    current_app.config_manager.update_smtp(data)

    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='smtp',
        detail=f'Updated SMTP settings (mode: {data["mode"]})',
        risk='safe', outcome='success',
    )

    # Restart SMTP relay
    action_id, err = current_app.apply_manager.apply_service_restart(
        ['smtp-relay'], f'Updated SMTP ({data["mode"]})', user, source_ip, risk='safe')
    flash('SMTP settings saved. SMTP relay restarting...', 'success')
    return redirect(url_for('settings.smtp'))


@settings_bp.route('/smtp/test', methods=['POST'])
def smtp_test():
    try:
        to_addr = request.form.get('to', 'admin@localhost')
        if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', to_addr):
            return jsonify({'success': False, 'message': 'Invalid email address.'}), 400
        result = subprocess.run(
            ['docker', 'exec', 'smtp-relay', 'sendmail', '-v', to_addr],
            input=f'Subject: razzfazz.ai SMTP Test\n\nThis is a test email from razzfazz.ai Configuration.\n',
            capture_output=True, text=True, timeout=15,
            cwd=current_app.config['STACK_ROOT'],
        )
        if result.returncode == 0:
            return jsonify({'success': True, 'message': 'Test email sent.'})
        return jsonify({'success': False, 'message': 'SMTP delivery failed. Check relay configuration.'})
    except Exception:
        return jsonify({'success': False, 'message': 'SMTP test failed. Is the smtp-relay container running?'})


# ---------------------------------------------------------------------------
# GPUStack
# ---------------------------------------------------------------------------

@settings_bp.route('/gpustack')
def gpustack():
    cfg = current_app.config_manager.get_gpustack_config()
    return render_template('settings/gpustack.html', config=cfg)


@settings_bp.route('/gpustack', methods=['POST'])
def gpustack_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    data = {
        'mode': request.form.get('mode'),
        'api_key': request.form.get('api_key', ''),
        'master_url': request.form.get('master_url', ''),
        'master_token': request.form.get('master_token', ''),
    }
    current_app.config_manager.update_gpustack(data)

    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='gpustack',
        detail=f'Updated GPUStack settings (mode: {data["mode"]})',
        risk='caution', outcome='success',
    )

    # Restart GPUStack + model-sync
    action_id, err = current_app.apply_manager.apply_service_restart(
        ['gpustack', 'model-sync'],
        f'Updated GPUStack (mode: {data["mode"]})', user, source_ip, risk='caution')
    flash('GPUStack settings saved. GPUStack restarting...', 'success')
    return redirect(url_for('settings.gpustack'))


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@settings_bp.route('/secrets')
def secrets_page():
    status = current_app.config_manager.get_secrets_status()
    needs_regen = sum(1 for s in status.values() if s['needs_regeneration'])
    return render_template('settings/secrets.html', secrets=status, needs_regen=needs_regen)


@settings_bp.route('/secrets/regenerate', methods=['POST'])
def secrets_regenerate():
    password = request.form.get('password', '')
    if password != current_app.config.get('ADMIN_PASSWORD', ''):
        flash('Invalid admin password.', 'error')
        return redirect(url_for('settings.secrets_page'))

    scope = request.form.get('scope', 'application')
    count = current_app.config_manager.regenerate_secrets(scope)
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    risk = 'danger' if scope == 'database' else 'caution'
    current_app.audit_logger.log(
        'secret.regenerate', user=user, source_ip=source_ip,
        category='secrets', action=f'regenerate_{scope}', target=scope,
        detail=f'Regenerated {count} {scope} secrets',
        risk=risk, outcome='success',
    )
    flash(f'Regenerated {count} {scope} secrets. Restart the stack to apply.', 'success')
    return redirect(url_for('settings.secrets_page'))


@settings_bp.route('/secrets/regenerate-one', methods=['POST'])
def secrets_regenerate_one():
    """Regenerate a single secret by key name."""
    password = request.form.get('password', '')
    if password != current_app.config.get('ADMIN_PASSWORD', ''):
        return jsonify({'error': 'Invalid admin password'}), 403

    key_name = request.form.get('key', '')
    count = current_app.config_manager.regenerate_single_secret(key_name)
    if count == 0:
        return jsonify({'error': f'Unknown secret: {key_name}'}), 400

    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    current_app.audit_logger.log(
        'secret.regenerate', user=user, source_ip=source_ip,
        category='secrets', action='regenerate_single', target=key_name,
        detail=f'Regenerated {key_name}',
        risk='caution', outcome='success',
    )
    return jsonify({'success': True, 'message': f'Regenerated {key_name}'})


# ---------------------------------------------------------------------------
# Personal Agents
# ---------------------------------------------------------------------------

AGENT_MANAGER_BASE = 'http://agent-manager:5000'


def _agent_api_get(path):
    """GET from the Agent Manager admin API. Returns (data, error)."""
    try:
        resp = http_requests.get(f'{AGENT_MANAGER_BASE}{path}', timeout=5,
                                    headers={'X-Authentik-Username': 'config-ui-admin'})
        resp.raise_for_status()
        return resp.json(), None
    except http_requests.ConnectionError:
        return None, 'Agent Manager is not running. Enable the agents profile first.'
    except Exception as exc:
        return None, str(exc)


def _agent_api_put(path, payload):
    """PUT to the Agent Manager admin API. Returns (data, error)."""
    try:
        resp = http_requests.put(
            f'{AGENT_MANAGER_BASE}{path}',
            json=payload, timeout=10,
            headers={'X-Authentik-Username': 'config-ui-admin'},
        )
        resp.raise_for_status()
        return resp.json(), None
    except http_requests.ConnectionError:
        return None, 'Agent Manager is not running.'
    except http_requests.HTTPError as exc:
        # The Agent Manager may return a non-JSON error body (e.g. a Flask HTML
        # 500 page). Decoding that blindly raised JSONDecodeError INSIDE this
        # handler and turned a clean error flash into a Config-UI 500.
        try:
            body = exc.response.json() if exc.response.content else {}
        except ValueError:
            body = {}
        return None, body.get('error', str(exc))
    except Exception as exc:
        return None, str(exc)


@settings_bp.route('/agents')
def agents():
    env_config = current_app.config_manager.get_agents_config()
    agent_types, types_err = _agent_api_get('/admin/api/types')
    quota_tiers, tiers_err = _agent_api_get('/admin/api/tiers')
    # #36 / PR #84 — DB-backed memory governance (global budget + per-user cap +
    # the DYNAMIC per-box host ceiling). Fetched live from the Agent Manager
    # admin API (persists in agent-manager's DB, not .env — live, no inode bug).
    mem_config, mem_err = _agent_api_get('/admin/api/config')
    return render_template('settings/agents.html',
                           config=env_config,
                           agent_types=agent_types,
                           types_err=types_err,
                           quota_tiers=quota_tiers,
                           tiers_err=tiers_err,
                           mem_config=mem_config or {},
                           mem_err=mem_err)


@settings_bp.route('/agents/limits', methods=['POST'])
def agents_limits_save():
    """Save global agent limits to .env."""
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    data = {
        'max_instances': request.form.get('max_instances', '20'),
        'idle_timeout_lightweight': request.form.get('idle_timeout_lightweight', '1800'),
        'idle_timeout_heavy': request.form.get('idle_timeout_heavy', '7200'),
        'cleanup_after_days': request.form.get('cleanup_after_days', '30'),
    }
    current_app.config_manager.update_agents_config(data)

    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='agents_limits',
        detail=f'Updated agent limits: max={data["max_instances"]}, cleanup={data["cleanup_after_days"]}d',
        risk='caution', outcome='success',
    )

    action_id, err = current_app.apply_manager.apply_service_restart(
        ['agent-manager'], 'Updated agent limits', user, source_ip, risk='caution')
    if err:
        flash(f'Settings saved but restart failed: {err}', 'error')
    else:
        flash('Agent limits saved. Agent Manager restarting...', 'success')
    return redirect(url_for('settings.agents'))


@settings_bp.route('/agents/memory', methods=['POST'])
def agents_memory_save():
    """#36 / PR #84 — save the agents memory budget + per-user cap.

    Persisted via the Agent Manager admin API (DB-backed agent_settings), NOT
    .env: it applies LIVE (the running provisioner reads it on the next launch —
    no agent-manager recreate) and avoids the known Config-UI single-file .env
    inode-rewrite bug. The global budget is CLAMPED server-side to the dynamic
    host ceiling (host RAM minus the core-stack reserve).

    Inputs are in GB from the form (operator-friendly); converted to MB for the
    admin API (which stores MB).
    """
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    def _gb_to_mb(field):
        raw = request.form.get(field, '').strip()
        if raw == '':
            return None
        try:
            return int(round(float(raw) * 1024))
        except ValueError:
            return None

    payload = {}
    budget_mb = _gb_to_mb('global_mem_budget_gb')
    per_user_mb = _gb_to_mb('per_user_mem_gb')
    if budget_mb is not None:
        payload['global_mem_budget_mb'] = budget_mb
    if per_user_mb is not None:
        payload['per_user_mem_mb'] = per_user_mb

    if not payload:
        flash('No memory settings to save.', 'error')
        return redirect(url_for('settings.agents'))

    _, err = _agent_api_put('/admin/api/config', payload)
    if err:
        flash(f'Failed to save memory budget: {err}', 'error')
    else:
        current_app.audit_logger.log(
            'config.change', user=user, source_ip=source_ip,
            category='settings', action='update', target='agents_memory_budget',
            detail=f'Updated agents memory governance: {payload}',
            risk='caution', outcome='success',
        )
        flash('Agents memory budget saved (applied live).', 'success')
    return redirect(url_for('settings.agents'))


@settings_bp.route('/agents/types/<type_id>', methods=['POST'])
def agents_type_save(type_id):
    """Update a single agent type via the Agent Manager admin API."""
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    payload = {
        'enabled': request.form.get('enabled') in ('true', 'on', '1'),
        'mem_limit': request.form.get('mem_limit', '256m'),
        'idle_timeout': int(request.form.get('idle_timeout', '1800')),
    }
    _, err = _agent_api_put(f'/admin/api/types/{type_id}', payload)
    if err:
        flash(f'Failed to update agent type: {err}', 'error')
    else:
        current_app.audit_logger.log(
            'config.change', user=user, source_ip=source_ip,
            category='settings', action='update', target=f'agent_type:{type_id}',
            detail=f'Updated agent type {type_id}: enabled={payload["enabled"]}, mem={payload["mem_limit"]}',
            risk='caution', outcome='success',
        )
        flash(f'Agent type "{type_id}" updated.', 'success')
    return redirect(url_for('settings.agents'))


@settings_bp.route('/agents/tiers/<tier_id>', methods=['POST'])
def agents_tier_save(tier_id):
    """Update a quota tier via the Agent Manager admin API."""
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    allowed_raw = request.form.get('allowed_types', '')
    allowed_types = [t.strip() for t in allowed_raw.split(',') if t.strip()] if allowed_raw else None

    payload = {
        'display_name': request.form.get('display_name', tier_id),
        'max_per_type': int(request.form.get('max_per_type', '1')),
        'max_heavy': int(request.form.get('max_heavy', '0')),
        'allowed_types': allowed_types,
        'priority': int(request.form.get('priority', '0')),
    }
    _, err = _agent_api_put(f'/admin/api/tiers/{tier_id}', payload)
    if err:
        flash(f'Failed to update quota tier: {err}', 'error')
    else:
        current_app.audit_logger.log(
            'config.change', user=user, source_ip=source_ip,
            category='settings', action='update', target=f'quota_tier:{tier_id}',
            detail=f'Updated tier {tier_id}: max_per_type={payload["max_per_type"]}, max_heavy={payload["max_heavy"]}',
            risk='caution', outcome='success',
        )
        flash(f'Quota tier "{payload["display_name"]}" updated.', 'success')
    return redirect(url_for('settings.agents'))


# ---------------------------------------------------------------------------
# MCP Integrations governance (#36 follow-up)
# ---------------------------------------------------------------------------
# Which per-user MCP integrations are AVAILABLE + their min tier
# (regular/power/admin). Persisted DB-backed in mcp-manager (NOT .env — no inode
# bug), reached via mcp-manager's token-gated /internal/governance endpoints.

MCP_MANAGER_BASE = 'http://mcp-manager:5000'


def _mcp_internal_headers():
    tok = os.environ.get('MCP_INTERNAL_TOKEN', '')
    return {'X-MCP-Internal-Token': tok} if tok else {}


def _mcp_api_get(path):
    try:
        resp = http_requests.get(f'{MCP_MANAGER_BASE}{path}', timeout=5,
                                 headers=_mcp_internal_headers())
        resp.raise_for_status()
        return resp.json(), None
    except http_requests.ConnectionError:
        return None, 'MCP Manager is not running. Enable the mcp profile first.'
    except Exception as exc:
        return None, str(exc)


def _mcp_api_put(path, payload):
    try:
        resp = http_requests.put(f'{MCP_MANAGER_BASE}{path}', json=payload,
                                 timeout=10, headers=_mcp_internal_headers())
        resp.raise_for_status()
        return resp.json(), None
    except http_requests.ConnectionError:
        return None, 'MCP Manager is not running.'
    except http_requests.HTTPError as exc:
        # The Agent Manager may return a non-JSON error body (e.g. a Flask HTML
        # 500 page). Decoding that blindly raised JSONDecodeError INSIDE this
        # handler and turned a clean error flash into a Config-UI 500.
        try:
            body = exc.response.json() if exc.response.content else {}
        except ValueError:
            body = {}
        return None, body.get('error', str(exc))
    except Exception as exc:
        return None, str(exc)


def _mcp_api_post(path, payload):
    try:
        resp = http_requests.post(f'{MCP_MANAGER_BASE}{path}', json=payload,
                                  timeout=20, headers=_mcp_internal_headers())
        resp.raise_for_status()
        return resp.json(), None
    except http_requests.ConnectionError:
        return None, 'MCP Manager is not running.'
    except http_requests.HTTPError as exc:
        # The Agent Manager may return a non-JSON error body (e.g. a Flask HTML
        # 500 page). Decoding that blindly raised JSONDecodeError INSIDE this
        # handler and turned a clean error flash into a Config-UI 500.
        try:
            body = exc.response.json() if exc.response.content else {}
        except ValueError:
            body = {}
        return None, body.get('error', str(exc))
    except Exception as exc:
        return None, str(exc)


@settings_bp.route('/mcp-integrations')
def mcp_integrations():
    """Governance page: enable/disable each MCP integration + set its min tier."""
    data, err = _mcp_api_get('/internal/governance')
    return render_template('settings/mcp_integrations.html',
                           integrations=(data or {}).get('integrations', []),
                           tiers=(data or {}).get('tiers', ['regular', 'power', 'admin']),
                           mcp_err=err)


@settings_bp.route('/mcp-integrations/<mcp_id>', methods=['POST'])
def mcp_integration_save(mcp_id):
    """Update one integration's availability + min tier (server-side enforced in
    mcp-manager; this only writes the governance row)."""
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    payload = {
        'available': request.form.get('available') in ('true', 'on', '1'),
        'min_tier': request.form.get('min_tier', 'regular'),
    }
    _, err = _mcp_api_put(f'/internal/governance/{mcp_id}', payload)
    if err:
        flash(f'Failed to update MCP integration: {err}', 'error')
    else:
        current_app.audit_logger.log(
            'config.change', user=user, source_ip=source_ip,
            category='settings', action='update', target=f'mcp_integration:{mcp_id}',
            detail=(f'Set MCP integration {mcp_id}: available={payload["available"]}, '
                    f'min_tier={payload["min_tier"]}'),
            risk='caution', outcome='success',
        )
        flash(f'MCP integration "{mcp_id}" updated (applied live).', 'success')
    return redirect(url_for('settings.mcp_integrations'))


@settings_bp.route('/mcp-integrations/company-brain', methods=['POST'])
def mcp_company_brain_create():
    """#36 two-tier cognee: create a shared Company Brain dataset (admin curates).

    Calls mcp-manager's token-gated /internal/company-brain, which creates the
    cognee dataset and records it against the cognee-company governance row so
    provisioned instances are scoped to it. WRITE stays gated to power+."""
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    name = (request.form.get('company_dataset') or '').strip()
    if not name:
        flash('Company brain dataset name required.', 'error')
        return redirect(url_for('settings.mcp_integrations'))
    _, err = _mcp_api_post('/internal/company-brain', {'name': name})
    if err:
        flash(f'Failed to create company brain: {err}', 'error')
    else:
        current_app.audit_logger.log(
            'config.change', user=user, source_ip=source_ip,
            category='settings', action='create', target=f'company_brain:{name}',
            detail=f'Created/curated shared Cognee company brain "{name}" (write gated to power+)',
            risk='caution', outcome='success',
        )
        flash(f'Company brain "{name}" ready (readable by entitled users; '
              f'write reserved for power/admin).', 'success')
    return redirect(url_for('settings.mcp_integrations'))


# ---------------------------------------------------------------------------
# RAG Models (Cognee + LightRAG) — migrated from razzfazz-setup (#22)
# ---------------------------------------------------------------------------

GPUSTACK_API = 'http://gpustack:9090'


def _read_gpustack_api_key():
    """Read GPUSTACK_API_KEY from .env (for the model-list dropdown)."""
    return current_app.config_manager.read_env().get('GPUSTACK_API_KEY', '')


def _gpustack_model_ids():
    """Return the list of model IDs GPUStack currently exposes, for the
    RAG model dropdowns. Returns [] on any failure (no key, gpustack down,
    profile disabled) — the page degrades to a free-text input."""
    api_key = _read_gpustack_api_key()
    if not api_key:
        return []
    try:
        req = urllib.request.Request(
            f'{GPUSTACK_API}/v1-openai/models',
            headers={'Authorization': f'Bearer {api_key}'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return [m['id'] for m in data.get('data', []) if m.get('id')]
    except Exception:
        return []


@settings_bp.route('/rag-models')
def rag_models():
    cfg = current_app.config_manager.get_rag_config()
    return render_template('settings/rag_models.html',
                           config=cfg, available_models=_gpustack_model_ids())


@settings_bp.route('/rag-models/cognee', methods=['POST'])
def rag_models_cognee_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    llm_model = request.form.get('llm_model', '').strip()
    embedding_model = request.form.get('embedding_model', '').strip()
    if not llm_model or not embedding_model:
        flash('Cognee requires both an LLM model and an embedding model.', 'error')
        return redirect(url_for('settings.rag_models'))

    current_app.config_manager.set_cognee_models(
        llm_model, embedding_model, request.form.get('embedding_dim', '768'))

    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='cognee_models',
        detail=f'Updated Cognee models: llm={llm_model}, embedding={embedding_model}',
        risk='caution', outcome='success',
    )

    action_id, err = current_app.apply_manager.apply_service_restart(
        ['cognee'], 'Updated Cognee models', user, source_ip, risk='caution')
    if err:
        flash(f'Settings saved but restart failed: {err}', 'error')
    else:
        flash('Cognee model settings saved. Cognee restarting...', 'success')
    return redirect(url_for('settings.rag_models'))


@settings_bp.route('/rag-models/lightrag', methods=['POST'])
def rag_models_lightrag_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    llm_model = request.form.get('llm_model', '').strip()
    embedding_model = request.form.get('embedding_model', '').strip()
    if not llm_model or not embedding_model:
        flash('LightRAG requires both an LLM model and an embedding model.', 'error')
        return redirect(url_for('settings.rag_models'))

    current_app.config_manager.set_lightrag_models(
        llm_model, embedding_model,
        rerank_binding=request.form.get('rerank_binding', 'null'),
        rerank_model=request.form.get('rerank_model', ''))

    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='settings', action='update', target='lightrag_models',
        detail=f'Updated LightRAG models: llm={llm_model}, embedding={embedding_model}',
        risk='caution', outcome='success',
    )

    action_id, err = current_app.apply_manager.apply_service_restart(
        ['lightrag'], 'Updated LightRAG models', user, source_ip, risk='caution')
    if err:
        flash(f'Settings saved but restart failed: {err}', 'error')
    else:
        flash('LightRAG model settings saved. LightRAG restarting...', 'success')
    return redirect(url_for('settings.rag_models'))


# ---------------------------------------------------------------------------
# Sideload a model (#184 P1 / WS7d) — self-service local-GGUF install
# ---------------------------------------------------------------------------
# The operator supplies a GGUF they obtained themselves (upload a .gguf / .zip,
# or point at a path already on the box). We validate the GGUF magic, stream it
# into the gpustack-data volume under local-models/ (via `docker exec gpustack`,
# since the config container does NOT mount that volume), and register it with
# GPUStack from the LOCAL file (source=local_path) — never HuggingFace. Works in
# all three network modes; the easy on-ramp for offline / air-gapped boxes.


def _sideload_allowed_roots():
    """Roots a local-path (Input B) sideload may read from, as seen by the
    razzfazz-config container. Defaults to the stack directory (mounted at its
    host path); operators who bind-mount a staging/USB directory into
    razzfazz-config can add it via RAZZFAZZ_SIDELOAD_DIRS (colon-separated)."""
    roots = [current_app.config.get('STACK_ROOT', '')]
    extra = os.environ.get('RAZZFAZZ_SIDELOAD_DIRS', '')
    roots += [p for p in extra.split(':') if p.strip()]
    return [r for r in roots if r]


def _sideload_from_upload(file_storage, staging_dir):
    """Stage an uploaded .gguf / .zip into ``staging_dir`` and return
    ``(config_visible_gguf_path, on_disk_filename)`` after the GGUF magic
    check. Raises SideloadError on anything that isn't a real GGUF."""
    if not file_storage or not file_storage.filename:
        raise SideloadError('No file was uploaded.')
    saved = os.path.join(staging_dir, 'upload.bin')
    file_storage.save(saved)  # werkzeug streams to disk (large files spool)
    if model_sideload.is_zip(saved, file_storage.filename):
        gguf_path = model_sideload.extract_gguf_from_zip(saved, staging_dir)
        filename = model_sideload.sanitize_gguf_filename(os.path.basename(gguf_path))
    else:
        gguf_path = saved
        filename = model_sideload.sanitize_gguf_filename(file_storage.filename)
    if not model_sideload.gguf_magic_ok(gguf_path):
        raise SideloadError('That file is not a valid GGUF (the GGUF magic is '
                            'missing). Nothing was installed.')
    return gguf_path, filename


def _sideload_from_local_path(raw_path):
    """Resolve an operator-supplied local path (file or directory) to a
    validated ``(config_visible_gguf_path, on_disk_filename)``."""
    real = model_sideload.resolve_local_source(raw_path, _sideload_allowed_roots())
    if os.path.isdir(real):
        ggufs = model_sideload.find_ggufs_in_dir(real)
        if not ggufs:
            raise SideloadError('No .gguf file was found directly in that '
                                'directory.')
        if len(ggufs) > 1:
            names = ', '.join(os.path.basename(g) for g in ggufs)
            raise SideloadError(f'That directory holds multiple .gguf files '
                                f'({names}). Point at a single .gguf file.')
        real = ggufs[0]
    filename = model_sideload.sanitize_gguf_filename(os.path.basename(real))
    if not model_sideload.gguf_magic_ok(real):
        raise SideloadError('That file is not a valid GGUF (the GGUF magic is '
                            'missing). Nothing was installed.')
    return real, filename


@settings_bp.route('/sideload-model')
def sideload_model():
    llm_enabled = bool({'llm', 'llm-legacy', 'llm-cpu'} &
                       set(current_app.profile_manager.get_enabled_profiles()))
    return render_template('settings/sideload_model.html',
                           categories=model_sideload.VALID_CATEGORIES,
                           llm_enabled=llm_enabled,
                           local_models_dir=model_sideload.LOCAL_MODELS_DIR,
                           stack_root=current_app.config.get('STACK_ROOT', ''))


@settings_bp.route('/sideload-model', methods=['POST'])
def sideload_model_save():
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    name = request.form.get('name', '').strip()
    category = request.form.get('category', 'llm').strip()
    source_type = request.form.get('source_type', 'upload').strip()
    staging_dir = None
    try:
        if source_type == 'local_path':
            gguf_path, filename = _sideload_from_local_path(
                request.form.get('local_path', ''))
        else:
            staging_dir = tempfile.mkdtemp(prefix='rzfz-sideload-')
            gguf_path, filename = _sideload_from_upload(
                request.files.get('model_file'), staging_dir)

        if not name:
            name = os.path.splitext(filename)[0]

        # Refuse to clobber an existing GGUF of the same name.
        if model_sideload.target_exists(filename):
            raise SideloadError(f'A model file named "{filename}" already '
                                f'exists in the gpustack volume. Rename the '
                                f'file or remove the existing one first.')

        # Free-space guard on the target volume before we write anything.
        avail = model_sideload.gpustack_available_bytes()
        size = os.path.getsize(gguf_path)
        if not model_sideload.has_enough_space(avail, size):
            gib = size / (1024 ** 3)
            avail_gib = (avail or 0) / (1024 ** 3)
            raise SideloadError(
                f'Not enough free space in the gpustack volume: the model is '
                f'{gib:.1f} GiB but only {avail_gib:.1f} GiB is free.')

        # Quarantine + atomic place into local-models/ (nothing half-written
        # survives on failure).
        model_sideload.cleanup_stale_quarantine()
        model_sideload.place_gguf(gguf_path, filename)

        # Register from the LOCAL file (source=local_path) — never HuggingFace.
        api_key = _read_gpustack_api_key()
        payload = model_sideload.build_register_payload(name, filename, category)
        model_id, err = gpustack_client.register_local_model(api_key, payload)
        if err:
            # The GGUF is in place but GPUStack rejected it — roll back the
            # file so we don't leave an unregistered orphan behind.
            model_sideload.remove_remote(
                f'{model_sideload.LOCAL_MODELS_DIR}/{filename}')
            raise SideloadError(f'The GGUF was staged into the volume but '
                                f'GPUStack registration failed: {err}')

        current_app.audit_logger.log(
            'config.change', user=user, source_ip=source_ip,
            category='settings', action='sideload', target='gpustack_model',
            detail=(f'Sideloaded model "{name}" (category={category}, '
                    f'file={filename}, source={source_type}) → GPUStack '
                    f'id={model_id}'),
            risk='caution', outcome='success',
        )
        flash(f'Model "{name}" sideloaded and registered with GPUStack '
              f'(from {filename}). It will load on the next scheduler pass.',
              'success')
    except SideloadError as exc:
        current_app.audit_logger.log(
            'config.change', user=user, source_ip=source_ip,
            category='settings', action='sideload', target='gpustack_model',
            detail=f'Sideload failed ({source_type}): {exc}',
            risk='caution', outcome='failure',
        )
        flash(str(exc), 'error')
    finally:
        if staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)
    return redirect(url_for('settings.sideload_model'))


# ---------------------------------------------------------------------------
# Factory Reset
# ---------------------------------------------------------------------------

@settings_bp.route('/factory-reset')
def factory_reset():
    return render_template('settings/factory_reset.html')


@settings_bp.route('/factory-reset', methods=['POST'])
def factory_reset_execute():
    password = request.form.get('password', '')
    confirmation = request.form.get('confirmation', '')

    if password != current_app.config.get('ADMIN_PASSWORD', ''):
        flash('Invalid admin password.', 'error')
        return redirect(url_for('settings.factory_reset'))

    if confirmation != 'DELETE':
        flash('Confirmation phrase incorrect. Type DELETE.', 'error')
        return redirect(url_for('settings.factory_reset'))

    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    current_app.audit_logger.log(
        'factory.reset', user=user, source_ip=source_ip,
        category='system', action='factory_reset', target='stack',
        detail='Factory reset initiated',
        risk='danger', outcome='success',
    )

    # Execute factory reset (this will kill this container too)
    action_id, err = current_app.apply_manager.apply_service_restart(
        [], 'Factory reset — docker compose down -v', user, source_ip,
        category='system', risk='danger')

    # The actual reset command
    import threading, subprocess
    def _do_reset():
        import time
        time.sleep(2)
        subprocess.run(
            ['docker', 'compose', 'down', '-v'],
            cwd=current_app.config['STACK_ROOT'],
        )
    threading.Thread(target=_do_reset, daemon=True).start()

    flash('Factory reset initiated. The stack is shutting down...', 'success')
    return redirect(url_for('settings.factory_reset'))
