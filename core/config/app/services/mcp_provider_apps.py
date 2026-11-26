# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3430 — what each personal MCP integration needs on this box, and the
provider OAuth apps an administrator enters on the Settings → MCP
Integrations page.

Read-only inputs, all from the stack tree the Portal already mounts read-only:

* core/mcp/personal-mcp-catalog.yaml — the integrations (one row each);
* core/mcp/oauth-providers.yaml — per provider the .env keys that carry the
  client id and secret (`client_id_env`, `client_secret_env`) and the URL
  variables a sign-in address is built from (M365_TENANT_ID);
* core/mcp/mcp-servers.yaml — `gateway_servers[].requires_profiles`, the
  modules an integration needs (a catalogue `engine_profile` is added to it);
* modules/mcp-manager/compose.yml — which services read a key, so a save
  recreates exactly those (and only the ones whose module is on).

Storage is the stack .env, written through ConfigManager's inode-preserving
writer (the file is a single-file bind mount here): mcp-manager and ms365-mcp
read these keys from their environment (compose interpolation, no env_file),
so a recreate is what makes a new value live. OpenBao is not used: neither
the Portal's AppRole nor mcp-manager's may read or write a provider-app path
there, and ms365-mcp takes its client id and tenant from the environment only.

Values never leave this module towards a page, a flash or the audit trail:
`rows()` reports per key only whether it is set. Every value is checked for a
shape that cannot break out of its .env line (no whitespace, quotes, `$` or
`#`) before it is written, and a key is written only when it is one of the
provider's own OAuth keys (`OAUTH_*`) or a known URL variable.
"""
from __future__ import annotations

import logging
import os
import re

import yaml

logger = logging.getLogger(__name__)

CATALOG_REL = os.path.join('core', 'mcp', 'personal-mcp-catalog.yaml')
PROVIDERS_REL = os.path.join('core', 'mcp', 'oauth-providers.yaml')
SERVERS_REL = os.path.join('core', 'mcp', 'mcp-servers.yaml')
COMPOSE_REL = os.path.join('modules', 'mcp-manager', 'compose.yml')

#: The module every personal integration runs in (the MCP manager itself).
BASE_PROFILE = 'mcp'

#: Help Center pages (docs/enterprise/<rel>), per OAuth provider; PAT and
#: no-credential integrations share the personal-MCP page.
HELP_PAGES = {
    'm365': 'how-to/mcp-app-m365.md',
    'google': 'how-to/mcp-app-google.md',
    'gitea': 'how-to/mcp-app-gitea.md',
    'github': 'how-to/mcp-app-github.md',
    'atlassian': 'how-to/mcp-app-atlassian.md',
}
DEFAULT_HELP_PAGE = 'how-to/personal-mcp.md'

#: Providers whose app the box registers itself (`rzfz post-install`). The
#: form stays: when the automatic registration is refused, the administrator
#: registers the app by hand and enters it here.
POST_INSTALL_REGISTERED = frozenset({'gitea'})

#: Provider `registration:` values that mean the box needs no app at all
#: (OAuth dynamic client registration, RFC 7591).
DYNAMIC_REGISTRATION = frozenset({'dynamic', 'dcr'})

_GUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
#: The same shape mcp-manager accepts for the tenant (app/services/oauth.py
#: _PATH_VARS): a tenant id or a tenant domain, never a multi-tenant authority.
TENANT_RE = re.compile(rf'^(?:{_GUID}|{_LABEL}(?:\.{_LABEL})+)$')
#: Not one organisation's tenant (the personal-account and first-party
#: tenants); mcp-manager refuses them too.
REFUSED_TENANTS = frozenset({
    '9188040d-6c67-4c5b-b112-36a304b66dad',
    'f8cdef31-a31e-4b4a-93e4-5f571e91255a',
})
#: A client id: Entra/Gitea GUIDs, Google `<n>-<x>.apps.googleusercontent.com`,
#: GitHub `Ov23…`/`Iv1.…`, Atlassian alphanumerics.
CLIENT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,254}$')
#: A client secret: what the providers issue (Entra uses `~ . _ -`, Google
#: `GOCSPX-…`, Gitea `gto_…`), plus base64's `+ / =`. Nothing that a .env line
#: or compose interpolation would read differently.
CLIENT_SECRET_RE = re.compile(r'^[A-Za-z0-9._~+/=-]{8,512}$')

#: URL variables an administrator enters with the app: (label, hint).
URL_FIELDS = {
    'M365_TENANT_ID': ('Directory (tenant) ID',
                       'The tenant id (GUID) or tenant domain of your organisation; '
                       'not common, organizations or consumers.'),
}
_URL_VAR = re.compile(r'\$\{([A-Z][A-Z0-9_]*)\}')
_OAUTH_KEY = re.compile(r'^OAUTH_[A-Z0-9_]{1,64}$')


class ProviderAppError(ValueError):
    """A value or provider the form refuses; the message never holds a value."""


def _load(stack_root, rel):
    path = os.path.join(stack_root, rel)
    try:
        with open(path, encoding='utf-8') as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        logger.warning('could not read %s', rel)
        return {}


def providers(stack_root):
    data = (_load(stack_root, PROVIDERS_REL).get('providers') or {})
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def client_id_key(name, p):
    """The .env key carrying `name`'s client id — `client_id_env`, else the
    OAUTH_<PROVIDER>_CLIENT_ID convention mcp-manager uses."""
    return str(p.get('client_id_env') or f'OAUTH_{name.upper()}_CLIENT_ID')


def client_secret_key(name, p):
    return str(p.get('client_secret_env') or f'OAUTH_{name.upper()}_CLIENT_SECRET')


def method_of(name, p):
    """'dynamic' (no app needed), 'automatic' (the box registers it) or 'app'."""
    if str(p.get('registration') or '').lower() in DYNAMIC_REGISTRATION:
        return 'dynamic'
    if name in POST_INSTALL_REGISTERED:
        return 'automatic'
    return 'app'


def fields(name, p):
    """The form's fields for provider `name`, in order: client id, client
    secret, then each URL variable it is built from. Raises ProviderAppError
    when the registry names a key this form may not write."""
    out = [{'field': 'client_id', 'key': client_id_key(name, p), 'label': 'Client ID',
            'secret': False, 'hint': ''},
           {'field': 'client_secret', 'key': client_secret_key(name, p), 'label': 'Client secret',
            'secret': True, 'hint': ''}]
    seen = set()
    for url_key in ('auth_url', 'token_url'):
        for var in _URL_VAR.findall(str(p.get(url_key) or '')):
            if var in URL_FIELDS and var not in seen:
                seen.add(var)
                label, hint = URL_FIELDS[var]
                out.append({'field': var.lower(), 'key': var, 'label': label,
                            'secret': False, 'hint': hint})
    for f in out[:2]:
        if not _OAUTH_KEY.fullmatch(f['key']):
            raise ProviderAppError(f'{name}: {f["key"]} is not an OAuth key this page may write')
    return out


def _valid(field, value):
    if field['field'] == 'client_id':
        return bool(CLIENT_ID_RE.fullmatch(value))
    if field['field'] == 'client_secret':
        return bool(CLIENT_SECRET_RE.fullmatch(value))
    if field['key'] == 'M365_TENANT_ID':
        return bool(TENANT_RE.fullmatch(value)) and value.lower() not in REFUSED_TENANTS
    return False


def _set(env, field, p):
    """Whether `field` has a usable value on this box (never the value)."""
    value = (env.get(field['key']) or '').strip()
    if field['field'] == 'client_id' and not value:
        value = str(p.get('client_id') or '').strip()   # literal id in the registry
    return bool(value) and (field['key'] not in URL_FIELDS or _valid(field, value))


def _requirements(stack_root):
    """mcp-servers.yaml gateway_servers: id → requires_profiles."""
    out = {}
    for s in (_load(stack_root, SERVERS_REL).get('gateway_servers') or []):
        if isinstance(s, dict) and s.get('id'):
            out[str(s['id'])] = [str(x) for x in (s.get('requires_profiles') or [])]
    return out


def help_url(help_domain, rel):
    """The Help Center address of docs/enterprise/<rel> (the Help Center
    serves the Enterprise tree under `enterprise/`, one id per page)."""
    if not help_domain:
        return ''
    slug = ('enterprise/' + rel[:-3]).replace('/', '__')
    return f'https://{help_domain}/docs/{slug}/'


METHOD_LABELS = {
    'app': 'App required',
    'automatic': 'Automatic registration',
    'dynamic': 'Automatic registration',
    'pat': 'Personal token',
    'none': 'Nothing to set up',
}


def rows(stack_root, env, enabled_profiles):
    """One row per catalogue integration. Values are never included."""
    catalogue = _load(stack_root, CATALOG_REL).get('integrations') or []
    provs = providers(stack_root)
    needs = _requirements(stack_root)
    domain = (env.get('MAIN_DOMAIN') or '').strip()
    help_domain = (env.get('HELP_DOMAIN') or '').strip()
    out = []
    for m in catalogue:
        if not isinstance(m, dict) or not m.get('id'):
            continue
        mid = str(m['id'])
        cred = str(m.get('cred_model') or 'none')
        profiles = list(needs.get(mid) or [BASE_PROFILE])
        if BASE_PROFILE not in profiles:
            profiles.insert(0, BASE_PROFILE)
        engine = m.get('engine_profile')
        if isinstance(engine, str) and engine and engine not in profiles:
            profiles.append(engine)
        modules = [{'profile': pr, 'on': pr in enabled_profiles} for pr in profiles]
        row = {'id': mid, 'display_name': str(m.get('display_name') or mid),
               'modules': modules, 'modules_on': all(x['on'] for x in modules),
               'provider': None, 'redirect_uri': '', 'form': None, 'missing': [],
               'help_url': help_url(help_domain, DEFAULT_HELP_PAGE)}
        if isinstance(m.get('remote_mcp'), dict):
            # A remote MCP server the box registers itself with (OAuth
            # dynamic client registration): nothing to enter, no redirect to
            # register.
            name = cred.split(':', 1)[1] if cred.startswith('oauth:') else mid
            row.update(method='dynamic', configured=True, provider=name,
                       help_url=help_url(help_domain, HELP_PAGES.get(
                           name, HELP_PAGES.get(name.split('-')[0], DEFAULT_HELP_PAGE))))
        elif cred.startswith('oauth:'):
            name = cred.split(':', 1)[1]
            p = provs.get(name)
            row['provider'] = name
            row['help_url'] = help_url(help_domain, HELP_PAGES.get(name, DEFAULT_HELP_PAGE))
            row['redirect_uri'] = (f'https://mcp.{domain}/oauth/callback/{name}' if domain else '')
            if p is None:
                row.update(method='app', configured=False,
                           missing=[f'provider {name} is not in the OAuth registry'])
            else:
                row['method'] = method_of(name, p)
                if row['method'] == 'dynamic':
                    row['configured'] = True
                else:
                    try:
                        fl = fields(name, p)
                    except ProviderAppError as exc:
                        row.update(configured=False, missing=[str(exc)])
                    else:
                        for f in fl:
                            f['set'] = _set(env, f, p)
                        row['form'] = {'provider': name, 'fields': fl}
                        row['missing'] = [f['label'] for f in fl if not f['set']]
                        row['configured'] = not row['missing']
        elif cred == 'pat':
            row.update(method='pat', configured=True)
        else:
            row.update(method='none', configured=True)
        row['method_label'] = METHOD_LABELS[row['method']]
        # The catalogue's `unavailable_reason`: listed, but usable on no box
        # (the MCP manager refuses it) — nothing to set up, no form.
        reason = m.get('unavailable_reason')
        row['unavailable'] = str(reason).strip() if isinstance(reason, str) and reason.strip() else ''
        if row['unavailable']:
            row.update(form=None, configured=False, missing=[], redirect_uri='')
        out.append(row)
    return out


def prepare_updates(stack_root, provider, form):
    """The .env updates a save of `provider`'s form makes. An empty field
    keeps its value ("replace only"). Raises ProviderAppError naming the
    field (never echoing the value)."""
    p = providers(stack_root).get(provider)
    if p is None or method_of(provider, p) == 'dynamic':
        raise ProviderAppError('This integration has no app to enter on this box.')
    updates = {}
    for f in fields(provider, p):
        value = (form.get(f['field']) or '').strip()
        if not value:
            continue
        if not _valid(f, value):
            raise ProviderAppError(f'{f["label"]} does not look valid; nothing was saved.')
        updates[f['key']] = value
    return updates


def clear_keys(stack_root, provider):
    """The keys a "remove" empties: the client id and secret (a URL
    variable such as the tenant is box configuration and stays)."""
    p = providers(stack_root).get(provider)
    if p is None or method_of(provider, p) == 'dynamic':
        raise ProviderAppError('This integration has no app to enter on this box.')
    return [f['key'] for f in fields(provider, p) if f['field'] in ('client_id', 'client_secret')]


def services_reading(stack_root, keys, enabled_profiles, *, disabled=False):
    """The mcp-manager compose services whose environment interpolates one of
    `keys` and whose every profile is enabled — `compose up <svc>` starts a
    service whatever its profile says, so a disabled one is never named.
    Compose SERVICE names (apply_service_restart passes them through).
    `disabled=True` returns the readers whose module is OFF instead."""
    spec = _load(stack_root, COMPOSE_REL).get('services') or {}
    out = []
    for name, svc in spec.items():
        if not isinstance(svc, dict):
            continue
        env = svc.get('environment') or {}
        values = env.values() if isinstance(env, dict) else env
        text = ' '.join(str(v) for v in values)
        if not any(re.search(r'\$\{' + re.escape(k) + r'(?::?-[^}]*)?\}', text) for k in keys):
            continue
        profs = svc.get('profiles') or []
        on = bool(profs) and all(pr in enabled_profiles for pr in profs)
        if profs and on != disabled:
            out.append(str(name))
    return out


# --- Microsoft 365 access ----------------------------------------------------------

#: The .env keys the MCP gateway reads for Microsoft 365 access, with the
#: rule of its guard (mcp-manager app/services/m365_guard.py `settings`): an
#: absent or empty key is its DEFAULT (unrestricted), any other spelling is
#: its STRICTEST value. The portal runs in another image and cannot import
#: the guard; tests/unit/razzfazz-config/test_3430_page_matches_the_m365_guard.py
#: pins this copy to the shipped module over every input.
M365_MODE_KEY = 'M365_MCP_MODE'
M365_MODES = ('read-only', 'read-write')
M365_MODE_DEFAULT, M365_MODE_STRICTEST = 'read-write', 'read-only'
#: key, form field, label, default, strictest, effective only in read-write.
M365_SWITCHES = (
    ('M365_MCP_ALLOW_SEND', 'allow_send', 'Allow sending mail', True, False, True),
    ('M365_MCP_SEND_DELETE_AGENTS', 'send_delete_agents',
     'Agents may send mail and delete (no person confirms)', True, False, False),
    ('M365_MCP_SEND_DELETE_WORKFLOWS', 'send_delete_workflows',
     'Workflows and technical users may send mail and delete (no person confirms)', True, False, False),
    ('M365_MCP_CONFIRM_SEND_DELETE', 'confirm_send_delete',
     'In chat, ask the person to confirm each send and delete', False, True, False),
)


def _m365_mode(raw, invalid):
    if raw in (None, ''):
        return M365_MODE_DEFAULT
    if raw in M365_MODES:
        return raw
    invalid.append(M365_MODE_KEY)
    return M365_MODE_STRICTEST


def _m365_flag(raw, key, default, strictest, invalid):
    if raw in (None, ''):
        return default
    if raw in ('true', 'false'):
        return raw == 'true'
    invalid.append(key)
    return strictest


def m365_access(env):
    """The Microsoft 365 access settings exactly as the gateway reads them
    (the guard's rule above; sending is off while read-only)."""
    invalid = []
    mode = _m365_mode(env.get(M365_MODE_KEY), invalid)
    switches = []
    for key, field, label, default, strictest, gated in M365_SWITCHES:
        value = _m365_flag(env.get(key), key, default, strictest, invalid)
        switches.append({'key': key, 'field': field, 'label': label, 'value': value,
                         'on': value and (mode == 'read-write' or not gated)})
    return {'mode': mode, 'switches': switches, 'invalid': invalid}


def m365_read_values(env):
    """key -> the value the readers take from it (the rule, no gating)."""
    acc = m365_access(env)
    out = {M365_MODE_KEY: acc['mode']}
    out.update({s['key']: s['value'] for s in acc['switches']})
    return out


def m365_scopes_widen(before, after):
    """Whether `after` asks people for Graph scopes `before` did not: write
    access (read-write) or sending (Mail.Send)."""
    def need(acc):
        send = next(s['on'] for s in acc['switches'] if s['key'] == 'M365_MCP_ALLOW_SEND')
        return {'write': acc['mode'] == 'read-write', 'send': send}
    b, a = need(before), need(after)
    return (a['write'] and not b['write']) or (a['send'] and not b['send'])


def container_names(stack_root, services):
    """compose service -> container name (`container_name`, else the service)."""
    spec = _load(stack_root, COMPOSE_REL).get('services') or {}
    return {s: str((spec.get(s) or {}).get('container_name') or s) for s in services}


_M365_KEYS = (M365_MODE_KEY,) + tuple(sw[0] for sw in M365_SWITCHES)
#: Prints only the M365 access keys of a container's environment (never the
#: rest: mcp-manager's holds the provider secrets), its state and command.
def _inspect_fmt(keys):
    return ('RUNNING={{.State.Running}}\nCMD={{json .Config.Cmd}}\n'
            '{{range .Config.Env}}{{$k := index (split . "=") 0}}{{if or '
            + ' '.join(f'(eq $k "{k}")' for k in keys)
            + '}}{{println .}}{{end}}{{end}}')


_INSPECT_FMT = _inspect_fmt(_M365_KEYS)


def inspect_m365(container, run=None, keys=_M365_KEYS):
    """(running, cmd, env) of `container` as docker reports it now, env holding
    only `keys` (the M365 access keys unless named). Raises RuntimeError
    naming why it could not be read; returns None when the container does
    not exist."""
    import json
    import subprocess
    from .compose_allowlist import compose_env
    run = run or subprocess.run
    fmt = _INSPECT_FMT if tuple(keys) == _M365_KEYS else _inspect_fmt(keys)
    r = run(['docker', 'inspect', '--format', fmt, container],
            capture_output=True, text=True, timeout=10, env=compose_env())
    if r.returncode != 0:
        if 'no such' in (r.stderr or '').lower():   # docker: "no such object: <name>"
            return None
        raise RuntimeError(f'docker inspect exited {r.returncode}: {(r.stderr or "").strip()[:200]}')
    running, cmd, env = False, [], {}
    for line in (r.stdout or '').splitlines():
        if line.startswith('RUNNING='):
            running = line[8:].strip() == 'true'
        elif line.startswith('CMD='):
            try:
                cmd = json.loads(line[4:]) or []
            except ValueError:
                cmd = []
        elif '=' in line:
            k, v = line.split('=', 1)
            if k in keys:
                env[k] = v
    return running, cmd, env


def _effective_from_container(cmd, env):
    """What a running container does: its own environment, read with the
    guard's rule (compose always passes the keys, with their defaults). A
    container started before the keys existed runs as its command says: an
    explicit `--read-only` argument is read-only."""
    env = dict(env)
    if M365_MODE_KEY not in env and '--read-only' in [str(c) for c in cmd]:
        env[M365_MODE_KEY] = 'read-only'
    return m365_access(env)


def m365_running(stack_root, configured, enabled_profiles, inspect=None):
    """Compare the saved access settings with what each running reader of a
    key actually does. Returns {'mismatches': [{service, container, setting,
    saved, running}], 'problems': [str], 'orphans': [{service, container}]};
    an orphan is a reader whose module is OFF but whose container still runs
    (a disable that did not finish), serving whatever it started with."""
    inspect = inspect or inspect_m365
    labels = {M365_MODE_KEY: 'Mode'}
    labels.update({sw[0]: sw[2] for sw in M365_SWITCHES})
    saved = {M365_MODE_KEY: configured['mode']}
    saved.update({s['key']: s['on'] for s in configured['switches']})
    mismatches, problems, orphans, seen = [], [], [], {}

    def look(name):
        if name not in seen:
            try:
                seen[name] = inspect(name)
            except Exception as exc:  # noqa: BLE001 - shown, never trusted as a match
                problems.append(f'{name}: {exc}')
                seen[name] = False
        return seen[name]

    for key in _M365_KEYS:
        services = services_reading(stack_root, [key], enabled_profiles)
        for svc, name in container_names(stack_root, services).items():
            got = look(name)
            if not got or not got[0]:
                continue      # unreadable (reported) or not running: nothing serves it
            eff = _effective_from_container(got[1], got[2])
            running = {M365_MODE_KEY: eff['mode']}
            running.update({s['key']: s['on'] for s in eff['switches']})
            if running[key] != saved[key]:
                fmt = (lambda v: v) if key == M365_MODE_KEY else (lambda v: 'on' if v else 'off')
                mismatches.append({'service': svc, 'container': name, 'setting': labels[key],
                                   'saved': fmt(saved[key]), 'running': fmt(running[key])})
    off = services_reading(stack_root, _M365_KEYS, enabled_profiles, disabled=True)
    for svc, name in container_names(stack_root, off).items():
        got = look(name)
        if got and got[0]:
            orphans.append({'service': svc, 'container': name})
    return {'mismatches': mismatches, 'problems': problems, 'orphans': orphans}


def mismatched_services(running):
    """The compose services a "Restart now" recreates: exactly the
    mismatched ones (never an orphan: recreating it would start it again)."""
    return sorted({m['service'] for m in running['mismatches']})


def m365_access_updates(form):
    """The .env values a save of the access form writes, always explicit.
    Raises ProviderAppError for a mode outside M365_MODES. In read-only the
    switches are not on the form (they act only in read-write) and keep
    their stored values."""
    mode = (form.get('mode') or '').strip()
    if mode not in M365_MODES:
        raise ProviderAppError('Choose read-only or read-write; nothing was saved.')
    updates = {M365_MODE_KEY: mode}
    if mode == 'read-write':
        for key, field, *_ in M365_SWITCHES:
            updates[key] = 'true' if form.get(field) in ('true', 'on', '1') else 'false'
    return updates


# --- Google Workspace access ---------------------------------------------------------

#: The .env keys the per-user Google proxy reads (mcp-proxy/server.py
#: `settings`), with its rule: the value is stripped; unset or empty is the
#: default (read-write, true), any other spelling than the exact one is the
#: strictest (read-only, false). The proxy gets them from mcp-manager's own
#: environment ({{policy:...}} in the catalogue's env_map), and the manager's
#: instance roll restarts a running proxy whose settings differ from it.
#: Pinned to the shipped server.py by
#: tests/unit/razzfazz-config/test_3467_page_matches_the_google_proxy.py.
GOOGLE_MODE_KEY = 'GOOGLE_MCP_MODE'
#: key, form field, label.
GOOGLE_SWITCHES = (
    ('GOOGLE_MCP_ALLOW_SEND', 'allow_send', 'Allow sending (mail, sharing, invitations)'),
    ('GOOGLE_MCP_SEND_DELETE_AGENTS', 'send_delete_agents',
     'Agents may send and delete (no person confirms)'),
)
GOOGLE_KEYS = (GOOGLE_MODE_KEY,) + tuple(k for k, _, _ in GOOGLE_SWITCHES)


def google_access(env):
    """The Google Workspace settings exactly as the proxy reads them.
    `value` is what the key says; `on` is what it allows: read-only refuses
    every write, send and delete, so nothing is sent while read-only."""
    invalid = []
    raw = (env.get(GOOGLE_MODE_KEY) or '').strip()
    if raw in ('', 'read-write'):
        mode = 'read-write'
    elif raw == 'read-only':
        mode = 'read-only'
    else:
        mode = 'read-only'
        invalid.append(GOOGLE_MODE_KEY)
    switches = []
    for key, field, label in GOOGLE_SWITCHES:
        v = (env.get(key) or '').strip()
        if v in ('', 'true'):
            value = True
        else:
            value = False
            if v != 'false':
                invalid.append(key)
        switches.append({'key': key, 'field': field, 'label': label, 'value': value,
                         'on': value and mode == 'read-write'})
    return {'mode': mode, 'switches': switches, 'invalid': invalid}


def google_read_values(env):
    acc = google_access(env)
    out = {GOOGLE_MODE_KEY: acc['mode']}
    out.update({s['key']: s['value'] for s in acc['switches']})
    return out


def google_access_updates(form):
    """The .env values a save of the Google card writes, always explicit.
    In read-only the switches are not on the form and keep their values."""
    mode = (form.get('mode') or '').strip()
    if mode not in ('read-only', 'read-write'):
        raise ProviderAppError('Choose read-only or read-write; nothing was saved.')
    updates = {GOOGLE_MODE_KEY: mode}
    if mode == 'read-write':
        for key, field, _ in GOOGLE_SWITCHES:
            updates[key] = 'true' if form.get(field) in ('true', 'on', '1') else 'false'
    return updates


def google_running(stack_root, saved_env, enabled_profiles, inspect=None):
    """Compare what each running reader of a Google key (mcp-manager) holds
    with the saved settings, both read with the proxy's rule. Same shape as
    m365_running (orphans are not looked for: mcp-manager is its own
    module)."""
    inspect = inspect or inspect_m365
    labels = {GOOGLE_MODE_KEY: 'Mode'}
    labels.update({k: label for k, _, label in GOOGLE_SWITCHES})
    saved = google_read_values(saved_env)
    mismatches, problems, seen = [], [], {}
    for key in GOOGLE_KEYS:
        services = services_reading(stack_root, [key], enabled_profiles)
        for svc, name in container_names(stack_root, services).items():
            if name not in seen:
                try:
                    seen[name] = inspect(name, keys=GOOGLE_KEYS)
                except Exception as exc:  # noqa: BLE001 - shown, never trusted as a match
                    problems.append(f'{name}: {exc}')
                    seen[name] = False
            got = seen[name]
            if not got or not got[0]:
                continue
            running = google_read_values(got[2])
            if running[key] != saved[key]:
                fmt = (lambda v: v) if key == GOOGLE_MODE_KEY else (lambda v: 'on' if v else 'off')
                mismatches.append({'service': svc, 'container': name, 'setting': labels[key],
                                   'saved': fmt(saved[key]), 'running': fmt(running[key])})
    return {'mismatches': mismatches, 'problems': problems, 'orphans': []}
