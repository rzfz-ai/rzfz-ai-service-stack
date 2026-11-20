# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1189 — stale single-file `.env` bind detection for the Portal.

#1224: the implementation moved to `razzfazz_common.env_mount` because the
backup-management container mounts the same two files the same way and needed
the same treatment. This module keeps the Portal's import path and the
Portal's defaults (`RECREATE_CMD` names razzfazz-config); see the shared
module for the mechanism.

Two things are Portal-only:

* #3058: `env_write_guard` refuses BEFORE the write when the bind is stale. The
  shared guard only translates a failed write, and a bind pinned to an orphaned
  inode does not fail: the write lands in a file the host never reads again.
* #3066: messages print the real stack directory, read from this container's
  own compose label.
"""
import os
import subprocess
import time
from contextlib import contextmanager

from razzfazz_common import env_mount as _shared
from razzfazz_common.env_mount import (  # noqa: F401
    DEFAULT_SERVICE, ENV_FILES, FIND_STACK_DIR, RECREATE_CMD, SERVICE_LABELS, _STALE_ERRNOS,
    StaleEnvMountError, _explain, explain_stale,
    explain_write_failure, host_command, inspect_env_file, recreate_cmd,
)

PORTAL_CONTAINER = 'razzfazz-config'
WORKING_DIR_LABEL = 'com.docker.compose.project.working_dir'

#: A daemon that does not answer is asked again after this long; a message must
#: not cost a five-second docker timeout each time it renders.
_NEGATIVE_TTL = 60

_label_cache = {'dir': None, 'retry_at': 0.0}


def stack_dir_from_label(run=subprocess.run, clock=time.monotonic):
    """The stack directory on the HOST, from this container's own compose label,
    or None. The label cannot change during the container's life, so a good
    answer is kept."""
    if _label_cache['dir']:
        return _label_cache['dir']
    if clock() < _label_cache['retry_at']:
        return None
    from .compose_allowlist import compose_env
    try:
        r = run(['docker', 'inspect', '--format',
                 '{{ index .Config.Labels "' + WORKING_DIR_LABEL + '" }}', PORTAL_CONTAINER],
                capture_output=True, text=True, timeout=5, env=compose_env())
        value = (r.stdout or '').strip() if r.returncode == 0 else ''
    except Exception:  # noqa: BLE001 - a message must not fail on its own hint
        value = ''
    if value.startswith('/') and '\n' not in value:
        _label_cache['dir'] = value
        return value
    _label_cache['retry_at'] = clock() + _NEGATIVE_TTL
    return None


def reset_stack_dir_cache():
    _label_cache.update({'dir': None, 'retry_at': 0.0})


_shared.set_stack_dir_resolver(lambda: stack_dir_from_label())


def inspect_env_mounts(stack_root: str, service: str = DEFAULT_SERVICE) -> dict:
    """Both env files, probed through THIS module's `inspect_env_file` name —
    so a caller (or test) that swaps the probe here is honoured."""
    return _shared.inspect_env_mounts(stack_root, service, inspect_file=inspect_env_file)


@contextmanager
def env_write_guard(path: str, service: str = DEFAULT_SERVICE):
    """A write to `path` happens only through a bind that reaches the host's
    file. A stale bind raises `StaleEnvMountError` before the write; a write
    that fails anyway is translated by the shared guard."""
    info = inspect_env_file(path)
    if info.get('stale'):
        code = info.get('write_errno') or 30  # EROFS: the class of failure this is
        raise StaleEnvMountError(explain_stale(path, os.strerror(code), service), path, code)
    with _shared.env_write_guard(path, service):
        yield
