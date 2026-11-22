# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Settings → Technical users (#2473 D7, #2476).

Lists the technical users (the owner shown by username), and lets a platform
administrator (the portal's own gate: config-portal/admin in the role
catalogue) create one, bind and unbind consumers, rotate its AppRole
secret_id, revoke it and reassign it to a new owner.

With OpenBao on (the openbao profile; `build_registry` wires the adapter,
`current_app.secret_id_rotator` renews the Dify secret_ids), a create or a manual rotate shows the
AppRole's role_id and secret_id ONCE, in the response to that POST: never in
a flash message (the session cookie is signed, not encrypted), never in a log.

With the mcp module on (#2472), every change also reaches the MCP gateway
(mcp-manager's mirror; the page reconciles it on every load), and each
technical user gets an "MCP gateway token" (for Dify or an agent, shown once
like the secret_id) and an "Upstream credential" for each gateway server it
is bound to (a PAT or client credentials, vaulted by mcp-manager under the
technical user's own key, never shown again).

A technical user bound to mcp-integration:gitea also gets "Create Gitea
service user" (#2554, gitea module on): the portal (which holds the Gitea
admin; mcp-manager never does) creates a restricted Gitea user of the same
name whose description names the owner, and mcp-manager vaults its one token
with the chosen scopes for the gateway's gitea server. The token is never
shown; every revoke deletes it in Gitea and prohibits the login.
"""
from __future__ import annotations

import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from app.services.gitea_admin import gitea_admin_for
from app.services.mcp_technical_mirror import MCPMirror, mcp_enabled
from app.services.technical_users import (AuthentikAPI, AuthentikUnavailable, TechnicalUserError,
                                          TechnicalUserRegistry)

technical_users_bp = Blueprint('technical_users', __name__, url_prefix='/settings/technical-users')

ONE_TIME_TEXT = ("Paste both into the Dify app's secret variables OPENBAO_ROLE_ID and OPENBAO_SECRET_ID. "
                 "The secret_id is valid for 24 hours or 1 000 logins; it is renewed every 12 hours for every "
                 "Dify app bound here whose draft is published. Anyone who can edit this Dify app can read its "
                 "secret_id: app editors hold this technical user's secrets.")
MCP_TOKEN_TEXT = ("Copy it now — it is not shown again. It replaces this technical user's earlier token for the "
                  "same consumer. Anyone holding it calls the MCP gateway as this technical user.")
#: The upstream-credential form's fields (f_<field>); mcp-manager accepts
#: these and no others.
CREDENTIAL_FIELDS = ('api_key', 'client_id', 'client_secret')


def _log(outcome, detail=''):
    """Arguments for current_app.audit_logger.log(...) — every route calls it
    itself, for a refusal as well as for a success (R-SEC-11). Never a secret."""
    return dict(user=session.get('admin_username', 'admin'),
                source_ip=request.headers.get('X-Forwarded-For', request.remote_addr),
                category='identity', detail=detail,
                risk='caution', outcome=outcome)


def openbao_on(app) -> bool:
    """OpenBao is wired when the openbao profile is enabled (read per call:
    enabling the profile needs no Portal restart). `app.openbao_admin` is
    the tests' seam for a fake adapter."""
    return getattr(app, 'openbao_admin', None) is not None or \
        'openbao' in set(app.profile_manager.get_enabled_profiles())


def build_registry(app, audit=None):
    """#2476: the registry with the OpenBao half (and its CIDR bound, D7)
    only when the openbao profile is enabled; the MCP mirror only with the
    mcp module (#2472); the Gitea admin only with mcp and gitea (#2554).
    The one seam the routes and the rotator use."""
    from app.services.openbao_admin import OpenBaoAdmin
    from app.services.secret_id_rotation import caddy_cidr
    profiles = set(app.profile_manager.get_enabled_profiles())
    bao, cidr = None, None
    if openbao_on(app):
        bao = getattr(app, 'openbao_admin', None) or OpenBaoAdmin()
        cidr = lambda: caddy_cidr(app.config_manager.read_env())  # noqa: E731
    mirror = MCPMirror() if mcp_enabled(profiles) else None
    env = app.config_manager.read_env()
    # #2554: the Gitea service-user half only with mcp and gitea on.
    return TechnicalUserRegistry(AuthentikAPI(env), bao, audit=audit,
                                 bound_cidr=cidr, mcp_mirror=mirror,
                                 gitea=gitea_admin_for(lambda k: env.get(k), profiles))


def _registry():
    """The registry for this request. Tests may set
    `current_app.technical_user_registry_factory(None)`. The routes audit
    themselves (success and refusal), so the registry gets no audit callback."""
    factory = getattr(current_app, 'technical_user_registry_factory', None)
    if factory is not None:
        return factory(None)
    return build_registry(current_app)


def _actor():
    return session.get('admin_username', 'admin')


def _dify_apps(bindings):
    return [b.split(':', 1)[1] for b in bindings or [] if b.startswith('dify-app:')]


def _record(name, bindings, approle):
    rotator = getattr(current_app, 'secret_id_rotator', None)
    if rotator is not None and approle and approle.get('secret_id_accessor'):
        rotator.record_set(name, _dify_apps(bindings), approle['secret_id_accessor'])


def _rotation_warnings():
    """{app_id: [warning, ...]} from the rotator's status file."""
    import os
    path = os.path.join(current_app.config.get('STACK_ROOT', '/stack'), '.openbao-state',
                        'secret-id-rotation.status')
    out = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                if line.startswith('WARN\t'):
                    text = line.split('\t', 1)[1].strip()
                    m = re.search(r"Dify app ([A-Za-z0-9._/-]+?)(?:'s\b|\s|$)", text)
                    if m:
                        out.setdefault(m.group(1), []).append(text)
    except OSError:
        pass
    return out


def _mcp_on(reg=None):
    if reg is not None:
        return getattr(reg, '_mcp', None) is not None
    return mcp_enabled(current_app.profile_manager.get_enabled_profiles())


def _render(users=None, error=None, one_time=None, status=200, minted=None, mcp_on=False, mcp_warning=None,
            gitea_on=False):
    bao_on = openbao_on(current_app)
    return render_template('settings/technical_users.html', users=users or [], error=error,
                           one_time=one_time, one_time_text=ONE_TIME_TEXT, openbao_on=bao_on,
                           warnings=_rotation_warnings() if bao_on else {},
                           minted=minted, mcp_token_text=MCP_TOKEN_TEXT, mcp_on=mcp_on,
                           mcp_warning=mcp_warning, gitea_on=gitea_on), status


def _fail(e, action, name):
    """Every failed page action: audited (outcome failure), answered with a
    clean message and a redirect — never a bare 500. A refusal changed
    nothing; an unexpected error may have stopped half-way, which the page
    then shows (a revoke offers "Retry revoke")."""
    current_app.audit_logger.log('config.change', action=f'technical_user.{action}', target=name,
                                 **_log('failure', type(e).__name__))
    if isinstance(e, TechnicalUserError):
        flash(str(e), 'error')
    elif isinstance(e, AuthentikUnavailable):
        flash('Authentik did not answer usably; the action did not complete. Check the list and try again.',
              'error')
    else:
        current_app.logger.exception('technical users: %s %s failed', action, name)
        flash(f'{action} of {name} failed ({type(e).__name__}); it may be incomplete. '
              'Check the list and retry.', 'error')
    return redirect(url_for('technical_users.index'))


def _safe_list(reg):
    """The list for a page that also shows a one-time secret: a failing list
    must not lose the secret behind an error."""
    try:
        return reg.list()
    except Exception:  # noqa: BLE001
        current_app.logger.exception('technical users: list after a change failed')
        return []


@technical_users_bp.route('')
@technical_users_bp.route('/')
def index():
    try:
        reg = _registry()
        users = reg.list()
    except Exception as e:  # noqa: BLE001 — audited, answered with the page
        current_app.audit_logger.log('config.change', action='technical_user.list', target='*',
                                     **_log('failure', type(e).__name__))
        if not isinstance(e, (AuthentikUnavailable, TechnicalUserError)):
            current_app.logger.exception('technical users: list failed')
        return _render(error='Authentik is not reachable with the bootstrap token; technical users '
                             'cannot be listed right now.')
    warning = _reconcile(reg)
    return _render(users=_with_last_call(reg, users), mcp_on=_mcp_on(reg), mcp_warning=warning,
                   gitea_on=getattr(reg, '_gitea', None) is not None)


def _with_last_call(reg, users):
    """Each technical user's latest MCP gateway call, from mcp-manager (the
    gateway records it per token). No time when it cannot be read: the
    list still shows."""
    ask = getattr(getattr(reg, '_mcp', None), 'last_calls', None)
    if not callable(ask):
        return users
    try:
        seen = ask()
    except Exception as e:  # noqa: BLE001 — the page must still show
        current_app.logger.warning('technical users: last MCP calls unreadable (%s)', type(e).__name__)
        return users
    for u in users:
        if isinstance(u, dict) and seen.get(u.get('username')):
            u['last_used'] = seen[u['username']]
    return users


def _reconcile(reg):
    """Bring the MCP gateway in line with Authentik; a warning for the page
    when that did not fully work (the page itself still shows)."""
    try:
        reg.reconcile_mcp()
    except Exception as e:  # noqa: BLE001 — the page must still show
        current_app.logger.warning('technical users: MCP gateway reconcile failed (%s)', type(e).__name__)
        return ('The MCP gateway could not be brought in line with this list right now; '
                'it is retried on the next page load and every few minutes.')
    return None


@technical_users_bp.route('/create', methods=['POST'])
def create():
    name = (request.form.get('name') or '').strip()
    owner = (request.form.get('owner') or '').strip()
    bindings = [b.strip() for b in (request.form.get('bindings') or '').replace(',', '\n').splitlines() if b.strip()]
    try:
        reg = _registry()
        out = reg.create(name, owner=owner, bindings=bindings, actor=_actor())
        _record(name, bindings, out.get('approle'))
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'create', name)
    current_app.audit_logger.log('config.change', action='technical_user.create', target=name,
                                 **_log('success', f'owner={owner}; bindings={bindings}'))
    if out.get('approle'):
        return _render(users=_safe_list(reg), one_time={'name': name, **out['approle']})
    flash(f'Technical user {name} created, owned by {owner}.', 'success')
    return redirect(url_for('technical_users.index'))


@technical_users_bp.route('/<name>/bind', methods=['POST'])
def bind(name):
    try:
        binding = (request.form.get('binding') or '').strip()
        _registry().bind(name, binding, actor=_actor())
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'bind', name)
    current_app.audit_logger.log('config.change', action='technical_user.bind', target=name, **_log('success', binding))
    flash(f'{name}: binding added.', 'success')
    return redirect(url_for('technical_users.index'))


@technical_users_bp.route('/<name>/unbind', methods=['POST'])
def unbind(name):
    try:
        binding = (request.form.get('binding') or '').strip()
        _registry().unbind(name, binding, actor=_actor())
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'unbind', name)
    current_app.audit_logger.log('config.change', action='technical_user.unbind', target=name,
                                 **_log('success', binding))
    flash(f'{name}: binding removed.', 'success')
    return redirect(url_for('technical_users.index'))


@technical_users_bp.route('/<name>/rotate', methods=['POST'])
def rotate(name):
    try:
        reg = _registry()
        out = reg.rotate_secret_id(name, actor=_actor())
        row = next((u for u in reg.list() if u['username'] == name), {})
        _record(name, row.get('bindings'), out)
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'rotate_secret_id', name)
    current_app.audit_logger.log('config.change', action='technical_user.rotate_secret_id', target=name,
                                 **_log('success'))
    return _render(users=_safe_list(reg), one_time={'name': name, **out})


@technical_users_bp.route('/<name>/revoke', methods=['POST'])
def revoke(name):
    try:
        _registry().revoke(name, actor=_actor())
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'revoke', name)
    current_app.audit_logger.log('config.change', action='technical_user.revoke', target=name, **_log('success'))
    flash(f'{name} revoked: deactivated, its tokens and AppRole removed. Its record stays.', 'success')
    return redirect(url_for('technical_users.index'))


@technical_users_bp.route('/<name>/reassign', methods=['POST'])
def reassign(name):
    owner = (request.form.get('owner') or '').strip()
    try:
        reg = _registry()
        out = reg.reassign(name, owner, actor=_actor())
        if out.get('approle'):
            row = next((u for u in reg.list() if u['username'] == name), {})
            _record(name, row.get('bindings'), out['approle'])
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'reassign', name)
    current_app.audit_logger.log('config.change', action='technical_user.reassign', target=name,
                                 **_log('success', f"owner={owner}; reactivated={out.get('reactivated')}"))
    if out.get('approle'):
        return _render(users=_safe_list(reg), one_time={'name': name, **out['approle']})
    flash(f'{name} now belongs to {owner}' + (' and is active again.' if out.get('reactivated') else '.'),
          'success')
    return redirect(url_for('technical_users.index'))


@technical_users_bp.route('/<name>/mcp-token', methods=['POST'])
def mcp_token(name):
    """A new MCP gateway token, shown ONCE in this response (never in a
    flash, the session or a log). Audited at risk 'danger': whoever holds it
    calls the gateway as this technical user."""
    consumer = (request.form.get('consumer') or '').strip()
    try:
        reg = _registry()
        token = reg.mcp_mint_token(name, consumer, actor=_actor())
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'mcp_token', name)
    current_app.audit_logger.log('config.change', action='technical_user.mcp_token', target=name,
                                 **dict(_log('success', f'consumer={consumer}'), risk='danger'))
    return _render(users=_safe_list(reg), mcp_on=True,
                   minted={'name': name, 'consumer': consumer, 'token': token})


@technical_users_bp.route('/<name>/mcp-credential/<mcp_id>', methods=['POST'])
def mcp_credential(name, mcp_id):
    """Vault an upstream credential (a PAT, or a client id and secret) for a
    gateway server this technical user is bound to. The values go to
    mcp-manager only; the audit entry names the fields, never their values."""
    fields = {k: (request.form.get(f'f_{k}') or '').strip() for k in CREDENTIAL_FIELDS}
    fields = {k: v for k, v in fields.items() if v}
    try:
        _registry().mcp_store_credential(name, mcp_id, fields, actor=_actor())
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'mcp_credential', name)
    current_app.audit_logger.log('config.change', action='technical_user.mcp_credential', target=name,
                                 **dict(_log('success', f'{mcp_id}: {sorted(fields)}'), risk='danger'))
    flash(f'{name}: the credential for {mcp_id} is stored in the MCP gateway. It is not shown again.', 'success')
    return redirect(url_for('technical_users.index'))



@technical_users_bp.route('/<name>/company-brain', methods=['POST'])
def company_brain(name):
    """#2472: the company brain for a technical user's Dify workflows
    (gateway server cognee-company-svc)."""
    try:
        _registry().mcp_company_brain(name, actor=_actor())
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'company_brain', name)
    current_app.audit_logger.log('config.change', action='technical_user.company_brain', target=name,
                                 **_log('success', 'cognee-company'))
    flash(f'{name}: company brain ready. Its Dify workflows read it through the MCP gateway server '
          f'cognee-company-svc.', 'success')
    return redirect(url_for('technical_users.index'))


@technical_users_bp.route('/<name>/gitea', methods=['POST'])
def gitea_user(name):
    """Create (or refresh) the technical user's Gitea service user with a
    token of the chosen scopes; an earlier token of it stops working."""
    scopes = [s.strip() for s in request.form.getlist('scopes') if s.strip()]
    try:
        _registry().gitea_service_user(name, scopes, actor=_actor())
    except Exception as e:  # noqa: BLE001 — every error is audited and answered cleanly
        return _fail(e, 'gitea_user', name)
    current_app.audit_logger.log('config.change', action='technical_user.gitea_user', target=name,
                                 **dict(_log('success', f'scopes={sorted(scopes)}'), risk='danger'))
    flash(f'{name}: Gitea service user ready, with a token for {", ".join(sorted(scopes))}. '
          f'Add {name} as a collaborator on exactly the repositories it may use.', 'success')
    return redirect(url_for('technical_users.index'))
