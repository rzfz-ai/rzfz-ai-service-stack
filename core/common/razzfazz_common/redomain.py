# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Whole-host re-domain rules shared by `rzfz setup --set-domain`
(cli/setup_lib/config_manager.py, which loads this file from the checkout) and
the Config Portal (core/config, which has razzfazz_common installed).

One rule: a host is rewritten only when it IS `<old>` or ends in `.<old>`.
`myold.example` is not `old.example`; paths, queries, userinfo and ports are
carried over byte for byte. When the new domain is itself a subdomain of the
old one (`corp.com` -> `ai.corp.com`), a host already under the new domain is
left alone, so a second pass over the same file changes nothing.

Pure functions: no file IO, nothing is logged (values can hold credentials).
The same rule runs in SQL (scripts/sql/redomain-*.sql) and in awk
(scripts/redomain-reconcile.sh).
"""
from __future__ import annotations

import re

_URL_RE = re.compile(r'^([A-Za-z][A-Za-z0-9+.\-]*://)([^/?#]*)(.*)$', re.DOTALL)


def _under(host: str, domain: str) -> bool:
    host, domain = host.lower(), domain.lower()
    return host == domain or host.endswith('.' + domain)


def redomain_host(host: str, old_domain: str, new_domain: str) -> str:
    """Return the host moved from `old_domain` to `new_domain`, or `host`
    unchanged when it is not a whole-host match (or is already moved)."""
    if not old_domain or not _under(host, old_domain):
        return host
    if _under(new_domain, old_domain) and _under(host, new_domain):
        return host
    return host[:len(host) - len(old_domain)] + new_domain


def redomain_url(token: str, old_domain: str, new_domain: str) -> str:
    """Rewrite the host of ONE absolute URL (scheme://[userinfo@]host[:port]rest).
    IPv6 literals, bare words and non-URLs are returned unchanged."""
    m = _URL_RE.match(token)
    if not m:
        return token
    scheme, netloc, rest = m.groups()
    userinfo, at, hostport = netloc.rpartition('@')
    # An IPv6 literal (`[::1]:80`) partitions to host `[`, which never
    # equals or ends with `.<old>`, so it falls through unchanged.
    host, colon, port = hostport.partition(':')
    new_host = redomain_host(host, old_domain, new_domain)
    if new_host == host:
        return token
    return f'{scheme}{userinfo}{at}{new_host}{colon}{port}{rest}'


def is_dify_url_key(key: str) -> bool:
    return key.endswith('_URL') or key.endswith('_ORIGINS')


def redomain_dify_values(raw: dict, old_domain: str, new_domain: str) -> dict:
    """{KEY: new raw value} for the .env.dify `*_URL` / `*_ORIGINS` keys whose
    comma-separated URLs name the old domain as a whole host. Quoting is kept."""
    updates = {}
    if not old_domain:
        return updates
    for key, val in raw.items():
        if not is_dify_url_key(key):
            continue
        quote = val[0] if len(val) >= 2 and val[0] in '"\'' and val[-1] == val[0] else ''
        body = val[1:-1] if quote else val
        new_body = ','.join(redomain_url(t, old_domain, new_domain) for t in body.split(','))
        if new_body != body:
            updates[key] = f'{quote}{new_body}{quote}'
    return updates


def is_stack_domain_key(key: str, non_stack: frozenset | set = frozenset()) -> bool:
    """A `*_DOMAIN` key that names a stack host. `*_OAUTH_DOMAIN` is the
    customer's IdP tenant / allow-list domain, never a stack host."""
    return (key.endswith('_DOMAIN') and key != 'MAIN_DOMAIN'
            and key not in non_stack and not key.endswith('_OAUTH_DOMAIN'))


def redomain_env_domains(expanded: dict, raw: dict, old_domain: str, new_domain: str,
                         non_stack: frozenset | set = frozenset()) -> dict:
    """{KEY: new value} for the derived .env `*_DOMAIN` keys. A key whose RAW
    value still templates `${MAIN_DOMAIN}` already propagates and is skipped
    (writing the expanded literal back would de-templatize it)."""
    updates = {}
    if not old_domain:
        return updates
    for key in expanded:
        if not is_stack_domain_key(key, non_stack):
            continue
        if '${MAIN_DOMAIN}' in raw.get(key, ''):
            continue
        val = expanded.get(key, '')
        new_val = redomain_host(val, old_domain, new_domain)
        if new_val != val:
            updates[key] = new_val
    return updates


_SERVER_NAME_RE = re.compile(r'^server_name:\s*"?([A-Za-z0-9][A-Za-z0-9.\-]*(?::\d{1,5})?)"?\s*$', re.M)


def matrix_pin_updates(env: dict, old_domain: str, stack_root: str) -> dict:
    """{'MATRIX_SERVER_NAME': name} when a re-domain would otherwise leave a Matrix
    box without its pin (#2981), else {}. The Synapse server_name is the suffix of
    every user ID and cannot change, so it must NOT follow MAIN_DOMAIN: the name
    already in force is written before MAIN_DOMAIN moves. That is the server_name
    of the rendered config when there is one, else the old domain (the only name
    the render ever derived). A box that never ran Matrix gets nothing: its first
    enable pins the then-current domain."""
    import os
    if (env.get('MATRIX_SERVER_NAME') or '').strip() or not old_domain:
        return {}
    rendered = os.path.join(stack_root, 'modules', 'apps', 'matrix', 'synapse', 'homeserver.yaml.rendered')
    name = ''
    try:
        with open(rendered, encoding='utf-8') as fh:
            m = _SERVER_NAME_RE.search(fh.read())
        name = m.group(1) if m else ''
    except OSError:
        pass
    if not name and 'matrix' in [p.strip() for p in (env.get('COMPOSE_PROFILES') or '').split(',')]:
        name = old_domain
    return {'MATRIX_SERVER_NAME': name} if name else {}
