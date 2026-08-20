# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Mac LLM backends blueprint (#168 P2) — manage the Mac Ollama gateway.

The whole panel is GUARDED on the ``mac-llm`` profile being enabled (otherwise
it renders an "enable the module first" notice). Super-admin gating is
inherited from the app-wide before_request middleware (app/auth.py) — no
per-route decorator, same as every other portal page.

What it does:
  * lists the configured Macs, parsed back OUT of the gateway ``config.yaml``
    (``_load_macs`` — inverse of the P1 ``gen_config.build_config``), and
    live-pings each Mac's Ollama ``/api/version`` for reachability (2s);
  * add / remove a Mac → rewrites ``config.yaml`` via the P1 config generator
    (reused verbatim, never duplicated) → restarts the gateway through the
    shared ``ApplyManager.apply_service_restart`` (same path as settings.*);
  * Tier-1 Mac model management over the Ollama HTTP API — NO SSH:
    pull (``POST /api/pull``), preload/keep-warm (``POST /api/generate`` with
    ``keep_alive:-1``), list (``GET /api/tags`` + ``GET /api/ps``), delete
    (``DELETE /api/delete``).

Tier-2 SSH host management is P3 (design §7) — NOT built here. The template
leaves a per-Mac "host management (SSH)" seam for it.

Design: .gsd/reports/2026.08-mac-ollama-gateway-design.md
Plan:   .gsd/reports/2026.08-mac-ollama-gateway-plan.md (Phase P2, Tasks 2.1–2.3)
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import urllib.request
import urllib.parse

import requests as http_requests
import yaml
from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

mac_backends_bp = Blueprint('mac_backends', __name__)

PROFILE = 'mac-llm'
GATEWAY_MODULE_REL = 'modules/llm/mac-gateway'
CONFIG_YAML_REL = 'modules/llm/mac-gateway/config.yaml'
GATEWAY_CONTAINER = 'llm-mac-gateway'
DEFAULT_OLLAMA_PORT = 11434

# Ollama HTTP API timeouts (seconds). Pull downloads a model → generous.
_HEALTH_TIMEOUT = 2.0
_PULL_TIMEOUT = 1800
_PRELOAD_TIMEOUT = 300
_LIST_TIMEOUT = 10
_DELETE_TIMEOUT = 30

# Validation: an Ollama model alias/tag charset (e.g. "mac-qwen3-coder",
# "qwen3-coder:30b", "llama3.1:8b"). Names shown in Open WebUI.
_ALIAS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
# A DNS hostname label set (RFC-1123-ish) — the non-IP branch of _valid_host.
_HOSTNAME_RE = re.compile(
    r'^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)'
    r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
)


# ---------------------------------------------------------------------------
# Paths + the P1 config generator (reused, never duplicated)
# ---------------------------------------------------------------------------

def _stack_root():
    return current_app.config['STACK_ROOT']


def _config_path():
    return os.path.join(_stack_root(), CONFIG_YAML_REL)


def _gen_config():
    """Load the P1 config generator from the mounted stack root.

    modules/llm/mac-gateway/gen_config.py is the single source of truth for the
    gateway's config.yaml shape. We import the ACTUAL file (it rides along in
    the /stack bind mount) instead of re-implementing build_config/dump_yaml —
    add/remove writes exactly what P1 (and any hand-generation) writes.
    """
    path = os.path.join(_stack_root(), GATEWAY_MODULE_REL, 'gen_config.py')
    spec = importlib.util.spec_from_file_location('mac_gateway_gen_config', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _profile_enabled():
    return PROFILE in current_app.profile_manager.get_enabled_profiles()


def _gateway_key_set():
    """True if MAC_GATEWAY_MASTER_KEY is non-empty in .env (grep, never source)."""
    try:
        from razzfazz_common.env_utils import read_env_key
    except Exception:  # pragma: no cover - lib always present in-container
        return None
    val = read_env_key(os.path.join(_stack_root(), '.env'), 'MAC_GATEWAY_MASTER_KEY')
    return bool(val)


# ---------------------------------------------------------------------------
# config.yaml <-> macs (pure)
# ---------------------------------------------------------------------------

def _parse_api_base(base):
    """('http://192.0.2.194:11434/v1') -> ('192.0.2.194', 11434). (None, None) on junk."""
    try:
        p = urllib.parse.urlparse(base or '')
        if not p.hostname:
            return None, None
        return p.hostname, p.port or DEFAULT_OLLAMA_PORT
    except Exception:
        return None, None


def _macs_from_config(cfg):
    """Inverse of gen_config.build_config: a LiteLLM config dict -> macs list.

    Groups model_list entries by api_base (ip:port) into one Mac each. The Mac
    ``name`` is NOT persisted by build_config, so it is reconstructed as the ip
    (the stable, round-trip-safe identity used for management + removal).
    Pure — no app/network/filesystem.
    """
    by_key = {}
    order = []
    for entry in (cfg or {}).get('model_list') or []:
        params = entry.get('litellm_params', {}) or {}
        ip, port = _parse_api_base(params.get('api_base'))
        if ip is None:
            continue
        key = (ip, port)
        if key not in by_key:
            by_key[key] = {'name': ip, 'ip': ip, 'port': port, 'models': []}
            order.append(key)
        model = params.get('model', '') or ''
        tag = model.split('/', 1)[1] if model.startswith('openai/') else model
        by_key[key]['models'].append({
            'alias': entry.get('model_name', ''),
            'ollama_tag': tag,
        })
    return [by_key[k] for k in order]


def _load_macs():
    """Read config.yaml and return the macs list (empty if absent)."""
    path = _config_path()
    if not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    return _macs_from_config(cfg)


def _save_macs(macs):
    """Regenerate config.yaml from macs via the P1 generator (atomic write)."""
    gc = _gen_config()
    text = gc.dump_yaml(gc.build_config(macs))
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _add_model(macs, ip, port, alias, ollama_tag):
    """Return a NEW macs list with (alias, ollama_tag) added to the ip:port Mac.

    Appends to an existing Mac (same ip:port) or creates one; de-dupes on alias.
    Pure — takes and returns plain data.
    """
    port = int(port)
    out = [{'name': m.get('name', m['ip']), 'ip': m['ip'], 'port': int(m['port']),
            'models': list(m['models'])} for m in macs]
    for m in out:
        if m['ip'] == ip and m['port'] == port:
            if any(x['alias'] == alias for x in m['models']):
                return out
            m['models'].append({'alias': alias, 'ollama_tag': ollama_tag})
            return out
    out.append({'name': ip, 'ip': ip, 'port': port,
                'models': [{'alias': alias, 'ollama_tag': ollama_tag}]})
    return out


def _remove_mac(macs, ip):
    """Return a NEW macs list with the ip Mac (all its models) dropped. Pure."""
    return [{'name': m.get('name', m['ip']), 'ip': m['ip'], 'port': int(m['port']),
             'models': list(m['models'])} for m in macs if m['ip'] != ip]


def _find_mac(macs, ident):
    """Find a Mac by its ip identity (route <mac> param / remove key)."""
    for m in macs:
        if m['ip'] == ident:
            return m
    return None


# ---------------------------------------------------------------------------
# Validation (pure)
# ---------------------------------------------------------------------------

def _valid_host(s):
    if not s:
        return False
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(s))


def _valid_port(s):
    try:
        n = int(s)
    except (TypeError, ValueError):
        return False
    return 1 <= n <= 65535


def _valid_alias(s):
    return bool(s) and bool(_ALIAS_RE.match(s))


# ---------------------------------------------------------------------------
# Live health + Tier-1 Ollama HTTP API (no SSH)
# ---------------------------------------------------------------------------

def _health(ip, port, timeout=_HEALTH_TIMEOUT):
    """Ping GET /api/version. Returns (reachable: bool, version: str|None)."""
    url = f'http://{ip}:{port}/api/version'
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (LAN host)
            if r.status == 200:
                data = json.loads(r.read().decode('utf-8'))
                return True, data.get('version')
    except Exception:
        pass
    return False, None


# --- pure request builders (URL + payload) — unit-tested, no network --------

def _ollama_pull_request(ip, port, model):
    return 'POST', f'http://{ip}:{port}/api/pull', {'name': model, 'stream': False}


def _ollama_preload_request(ip, port, model):
    # keep_alive:-1 pins the model resident (kills cold-start); empty prompt
    # just triggers the load without generating.
    return ('POST', f'http://{ip}:{port}/api/generate',
            {'model': model, 'prompt': '', 'keep_alive': -1})


def _ollama_tags_request(ip, port):
    return 'GET', f'http://{ip}:{port}/api/tags', None


def _ollama_ps_request(ip, port):
    return 'GET', f'http://{ip}:{port}/api/ps', None


def _ollama_delete_request(ip, port, model):
    return 'DELETE', f'http://{ip}:{port}/api/delete', {'name': model}


def _ollama_call(method, url, payload, timeout):
    """Dispatch one Ollama HTTP call. Returns (ok, data_or_text, status)."""
    try:
        resp = http_requests.request(
            method, url,
            json=payload if payload is not None else None,
            timeout=timeout,
        )
    except http_requests.RequestException as e:
        return False, str(e), None
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.ok, data, resp.status_code


def _wants_json():
    return (
        request.headers.get('HX-Request')
        or request.args.get('format') == 'json'
        or request.accept_mimetypes.best == 'application/json'
    )


def _audit(action, target, detail, risk='caution', outcome='success'):
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='mac-llm', action=action, target=target,
        detail=detail, risk=risk, outcome=outcome,
    )
    return user, source_ip


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@mac_backends_bp.route('/')
def index():
    if not _profile_enabled():
        return render_template('mac_backends/index.html', enabled=False,
                               macs=[], gateway_key_set=None)
    rows = []
    for mac in _load_macs():
        reachable, version = _health(mac['ip'], mac['port'])
        rows.append({'mac': mac, 'reachable': reachable, 'version': version,
                     'models': mac['models']})
    return render_template('mac_backends/index.html', enabled=True,
                           macs=rows, gateway_key_set=_gateway_key_set())


@mac_backends_bp.route('/add', methods=['POST'])
def add():
    if not _profile_enabled():
        flash('Enable the mac-llm module first.', 'error')
        return redirect(url_for('mac_backends.index'))

    ip = (request.form.get('ip') or '').strip()
    port = (request.form.get('port') or str(DEFAULT_OLLAMA_PORT)).strip()
    alias = (request.form.get('alias') or '').strip()
    tag = (request.form.get('ollama_tag') or '').strip()

    if not _valid_host(ip):
        flash(f'Invalid Mac IP / hostname: {ip!r}', 'error')
        return redirect(url_for('mac_backends.index'))
    if not _valid_port(port):
        flash(f'Invalid port: {port!r} (1–65535)', 'error')
        return redirect(url_for('mac_backends.index'))
    if not _valid_alias(alias):
        flash(f'Invalid model alias: {alias!r}', 'error')
        return redirect(url_for('mac_backends.index'))
    if not _valid_alias(tag):
        flash(f'Invalid Ollama tag: {tag!r}', 'error')
        return redirect(url_for('mac_backends.index'))

    macs = _add_model(_load_macs(), ip, int(port), alias, tag)
    _save_macs(macs)
    user, source_ip = _audit(
        'add', f'{ip}:{port}',
        f'Added Mac backend {ip}:{port} model {alias} ({tag})')

    action_id, err = current_app.apply_manager.apply_service_restart(
        [GATEWAY_CONTAINER], f'Updated Mac LLM backends (add {alias}@{ip})',
        user, source_ip, category='mac-llm', risk='caution')
    if err:
        flash(f'Backend saved but gateway restart failed: {err}', 'error')
    else:
        flash(f'Added {alias} on {ip}:{port}. Gateway reloading...', 'success')
    return redirect(url_for('mac_backends.index'))


@mac_backends_bp.route('/remove', methods=['POST'])
def remove():
    if not _profile_enabled():
        flash('Enable the mac-llm module first.', 'error')
        return redirect(url_for('mac_backends.index'))

    ip = (request.form.get('ip') or '').strip()
    macs = _load_macs()
    if _find_mac(macs, ip) is None:
        flash(f'No configured Mac backend at {ip}.', 'error')
        return redirect(url_for('mac_backends.index'))

    _save_macs(_remove_mac(macs, ip))
    user, source_ip = _audit('remove', ip, f'Removed Mac backend {ip}')

    action_id, err = current_app.apply_manager.apply_service_restart(
        [GATEWAY_CONTAINER], f'Updated Mac LLM backends (remove {ip})',
        user, source_ip, category='mac-llm', risk='caution')
    if err:
        flash(f'Backend removed but gateway restart failed: {err}', 'error')
    else:
        flash(f'Removed Mac backend {ip}. Gateway reloading...', 'success')
    return redirect(url_for('mac_backends.index'))


def _resolve_mac_or_400(mac_ident):
    """Shared guard for the Tier-1 routes: profile on + Mac exists → the Mac dict."""
    if not _profile_enabled():
        return None, ('Enable the mac-llm module first.', 400)
    mac = _find_mac(_load_macs(), mac_ident)
    if mac is None:
        return None, (f'No configured Mac backend at {mac_ident}.', 404)
    return mac, None


def _tier1_respond(ok, summary, data, redirect_flash='success'):
    """JSON for HTMX/API callers; flash + redirect for the plain-form baseline."""
    if _wants_json():
        return jsonify({'ok': ok, 'summary': summary, 'data': data}), (200 if ok else 502)
    flash(summary, redirect_flash if ok else 'error')
    return redirect(url_for('mac_backends.index'))


@mac_backends_bp.route('/<mac>/pull', methods=['POST'])
def pull_model(mac):
    """Tier-1: pull a model onto the Mac (POST /api/pull)."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return (jsonify({'ok': False, 'summary': err[0]}), err[1]) if _wants_json() else (
            flash(err[0], 'error') or redirect(url_for('mac_backends.index')))
    model = (request.form.get('model') or '').strip()
    if not _valid_alias(model):
        return _tier1_respond(False, f'Invalid model name: {model!r}', None)
    method, url, payload = _ollama_pull_request(m['ip'], m['port'], model)
    ok, data, status = _ollama_call(method, url, payload, _PULL_TIMEOUT)
    _audit('pull', f"{m['ip']}", f'Pull {model} on {m["ip"]}',
           outcome='success' if ok else 'failure')
    summary = f'Pulled {model} on {m["ip"]}.' if ok else f'Pull of {model} failed: {data}'
    return _tier1_respond(ok, summary, data)


@mac_backends_bp.route('/<mac>/preload', methods=['POST'])
def preload_model(mac):
    """Tier-1: preload / keep-warm a model (POST /api/generate keep_alive:-1)."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return (jsonify({'ok': False, 'summary': err[0]}), err[1]) if _wants_json() else (
            flash(err[0], 'error') or redirect(url_for('mac_backends.index')))
    model = (request.form.get('model') or '').strip()
    if not _valid_alias(model):
        return _tier1_respond(False, f'Invalid model name: {model!r}', None)
    method, url, payload = _ollama_preload_request(m['ip'], m['port'], model)
    ok, data, status = _ollama_call(method, url, payload, _PRELOAD_TIMEOUT)
    _audit('preload', f"{m['ip']}", f'Preload {model} on {m["ip"]}',
           outcome='success' if ok else 'failure')
    summary = (f'Preloaded {model} (resident) on {m["ip"]}.' if ok
               else f'Preload of {model} failed: {data}')
    return _tier1_respond(ok, summary, data)


@mac_backends_bp.route('/<mac>/models', methods=['GET'])
def list_models(mac):
    """Tier-1: list installed (/api/tags) + running (/api/ps) models. JSON."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return jsonify({'ok': False, 'summary': err[0]}), err[1]
    _, tags_url, _ = _ollama_tags_request(m['ip'], m['port'])
    _, ps_url, _ = _ollama_ps_request(m['ip'], m['port'])
    tags_ok, tags, _ = _ollama_call('GET', tags_url, None, _LIST_TIMEOUT)
    ps_ok, ps, _ = _ollama_call('GET', ps_url, None, _LIST_TIMEOUT)
    return jsonify({
        'ok': tags_ok and ps_ok,
        'ip': m['ip'],
        'tags': tags if tags_ok else None,
        'ps': ps if ps_ok else None,
    }), (200 if (tags_ok and ps_ok) else 502)


@mac_backends_bp.route('/<mac>/delete-model', methods=['POST'])
def delete_model(mac):
    """Tier-1: delete a model from the Mac (DELETE /api/delete)."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return (jsonify({'ok': False, 'summary': err[0]}), err[1]) if _wants_json() else (
            flash(err[0], 'error') or redirect(url_for('mac_backends.index')))
    model = (request.form.get('model') or '').strip()
    if not _valid_alias(model):
        return _tier1_respond(False, f'Invalid model name: {model!r}', None)
    method, url, payload = _ollama_delete_request(m['ip'], m['port'], model)
    ok, data, status = _ollama_call(method, url, payload, _DELETE_TIMEOUT)
    _audit('delete-model', f"{m['ip']}", f'Delete {model} on {m["ip"]}',
           risk='danger', outcome='success' if ok else 'failure')
    summary = (f'Deleted {model} from {m["ip"]}.' if ok
               else f'Delete of {model} failed: {data}')
    return _tier1_respond(ok, summary, data)
