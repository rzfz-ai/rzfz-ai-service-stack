# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The box's time source as a setting, for the Settings portal.

The setting is ``TIME_SOURCE`` (internet | dhcp | lan | none) and ``TIME_SERVERS``
(``lan`` only) in ``.env``. The portal validates the input with the same rules as
scripts/apply-time-source.sh, saves the two keys and queues a request file for the
host: the container has no way to edit the host's chrony configuration (the stack
tree is mounted read-only and the process is not root). A systemd path unit on the
host (scripts/time-source-watcher.sh) re-validates the request as data, applies it
as root and writes ``time-source.result`` (applied / failed / rejected) next to it,
which this page shows. ``sudo rzfz setup --time-source`` is the manual fallback.
"""
from __future__ import annotations

import json
import os
import re
import time

MODES = ('internet', 'dhcp', 'lan', 'none')
DEFAULT_MODE = 'internet'
PENDING_NAME = 'time-source.pending'
RESULT_NAME = 'time-source.result'
APPLY_COMMAND = 'sudo rzfz setup --time-source'

_IPV4 = re.compile(r'[0-9]{1,3}(\.[0-9]{1,3}){3}')
_IPV6 = re.compile(r'(?=.*:)[0-9A-Fa-f:]+')
_HOST = re.compile(r'[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*')


def valid_server(value):
    """An IPv4 / IPv6 address or a host name, as scripts/apply-time-source.sh accepts."""
    if not value or len(value) > 253:
        return False
    if _IPV4.fullmatch(value):
        return all(int(octet) <= 255 for octet in value.split('.'))
    if _IPV6.fullmatch(value):
        return True
    return bool(_HOST.fullmatch(value))


def parse_servers(text):
    """Split a form value into unique servers. Returns ``(servers, bad)``; ``bad`` is
    the first invalid entry or ``None``. A control character is never a separator."""
    text = text or ''
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return [], '(control character)'
    servers = []
    for token in re.split(r'[\s,;]+', text.strip()):
        if not token:
            continue
        if not valid_server(token):
            return [], token
        if token not in servers:
            servers.append(token)
    return servers, None


def validate(mode, servers_text):
    """``(mode, servers, error)``: ``error`` is a message for the operator or ``None``."""
    mode = (mode or '').strip()
    if mode not in MODES:
        return mode, [], 'Choose a time source: internet, dhcp, lan or none.'
    servers, bad = parse_servers(servers_text)
    if bad is not None:
        return mode, [], f'"{bad}" is not an IP address or a host name.'
    if mode == 'lan' and not servers:
        return mode, [], 'The LAN time source needs at least one server (an IP address or a host name).'
    return mode, servers, None


def read(config_manager):
    env = config_manager.read_env() if config_manager is not None else {}
    mode = (env.get('TIME_SOURCE') or DEFAULT_MODE).strip()
    return {'mode': mode if mode in MODES else DEFAULT_MODE,
            'servers': (env.get('TIME_SERVERS') or '').replace(',', ' ').strip()}


def _pending_path(stack_root):
    return os.path.join(stack_root, '.probe-queue', PENDING_NAME)


def pending(stack_root):
    """The queued request, or ``None``."""
    try:
        with open(_pending_path(stack_root), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def result(stack_root):
    """The host's verdict on the last request: ``{'status', 'mode', 'reason', 'at'}`` or ``None``."""
    out = {}
    try:
        with open(os.path.join(stack_root, '.probe-queue', RESULT_NAME), encoding='utf-8') as fh:
            for line in fh.read(2048).splitlines():
                key, _, value = line.partition('=')
                out[key] = value
    except OSError:
        return None
    return out if out.get('status') in ('applied', 'failed', 'rejected') else None


def save(config_manager, stack_root, mode, servers):
    """Write the setting and queue the host request. Servers are kept for the other
    modes too, so switching back to ``lan`` finds them again."""
    config_manager.update_env_var('TIME_SOURCE', mode)
    config_manager.update_env_var('TIME_SERVERS', ' '.join(servers))
    request = {'mode': mode, 'servers': servers, 'requested_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    path = _pending_path(stack_root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # the verdict on the previous request does not describe this one
        try:
            os.unlink(os.path.join(os.path.dirname(path), RESULT_NAME))
        except FileNotFoundError:
            pass
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(request, fh)
    except OSError:
        return False
    return True
