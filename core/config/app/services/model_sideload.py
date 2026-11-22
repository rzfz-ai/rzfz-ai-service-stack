# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Sideload-a-model helpers — the Config-UI self-service GGUF install path.

#184 P1 / WS7d. The operator provides a GGUF they obtained themselves — either
an **upload** (a ``.gguf``, or a ``.zip`` containing exactly one) OR a **path to
a file/directory already on the box** (a staged folder under the stack dir, a
bind-mounted USB, …). We validate it is a real GGUF, stream it into the
``gpustack-data`` volume under :data:`LOCAL_MODELS_DIR` (via the gpustack
container — the config container does NOT mount that volume), and register it
with GPUStack from the **local file** (``source: local_path``), never
HuggingFace. Works in all three network modes (online / proxied / offline); it
is the easy on-ramp for offline / air-gapped boxes especially.

Design constraints this module respects:

* **Reuse, don't reinvent.** The registration ``source`` spec comes from
  :func:`model_source.model_source_fields` (the one place that decides
  online-HF vs offline-local_path — ``core/llm/model_source.py``, COPY'd into
  the config image at ``/app/model_source.py``). :data:`LOCAL_MODELS_DIR`
  mirrors ``model_source.LOCAL_MODELS_DIR`` /
  ``scripts/lib.sh::RAZZFAZZ_LOCAL_MODELS_DIR``.
* **Docker via the socket-proxy.** The config container talks to Docker through
  ``DOCKER_HOST=tcp://docker-socket-proxy:2375``, whose allowlist grants
  ``POST`` + ``EXEC`` (not the archive/PUT endpoint ``docker cp`` needs). So the
  volume write goes through ``docker exec -i gpustack sh -c 'cat > …'`` — the
  same POST+EXEC surface the SMTP-test route already uses — NOT ``docker cp``.
* **Quarantine before accept.** The GGUF magic is checked on the config-side
  staged/local file BEFORE anything is written to the volume; the volume write
  itself lands on a hidden ``.part`` quarantine file and is only atomically
  ``mv``'d into ``local-models/<filename>`` once the transfer is size-verified.
  On any failure nothing half-written survives under ``local-models/``.

The pure helpers (:func:`is_gguf_magic`, :func:`gguf_magic_ok`,
:func:`extract_gguf_from_zip`, :func:`sanitize_gguf_filename`,
:func:`resolve_local_source`, :func:`build_register_payload`) are Docker-free
and unit-tested directly. The ``gpustack_*`` transfer helpers shell out to
``docker`` via :func:`subprocess.run` (tests patch it).
"""
from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import zipfile

import model_source  # /app/model_source.py in the image (COPY'd from core/llm)

# The gpustack service is addressed by the fixed container_name `gpustack` on
# every LLM profile (llm / llm-legacy / llm-cpu are mutually exclusive and all
# use container_name: gpustack — modules/llm/compose.yml). The gpustack-data
# volume mounts at /var/lib/gpustack there.
GPUSTACK_CONTAINER = 'gpustack'
GPUSTACK_VOLUME_ROOT = '/var/lib/gpustack'
LOCAL_MODELS_DIR = model_source.LOCAL_MODELS_DIR  # /var/lib/gpustack/local-models

GGUF_MAGIC = b'GGUF'
VALID_CATEGORIES = ('llm', 'embedder', 'reranker')

# Free-space safety margin over the raw GGUF size (metadata, fs overhead).
_FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024  # 256 MiB


class SideloadError(Exception):
    """Operator-facing sideload failure — the message is shown in the UI."""


# ---------------------------------------------------------------------------
# Pure validation / naming helpers (Docker-free, directly unit-tested)
# ---------------------------------------------------------------------------

def is_gguf_magic(data) -> bool:
    """True when ``data`` begins with the 4-byte GGUF magic (``b'GGUF'``)."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    return bytes(data[:4]) == GGUF_MAGIC


def gguf_magic_ok(path: str) -> bool:
    """True when the file at ``path`` begins with the GGUF magic. False on any
    read error (missing / unreadable) — the caller turns that into a clear
    rejection."""
    try:
        with open(path, 'rb') as fh:
            return is_gguf_magic(fh.read(4))
    except OSError:
        return False


def sanitize_gguf_filename(name: str) -> str:
    """Reduce ``name`` to a safe ``<basename>.gguf`` (no path components, no
    traversal). Raises :class:`SideloadError` on anything that isn't a plain
    ``.gguf`` basename — the on-disk name under ``local-models/`` must never
    carry a directory or ``..``."""
    raw = str(name or '').strip().replace('\\', '/')
    base = os.path.basename(raw)
    if not base or base in ('.', '..'):
        raise SideloadError('Could not derive a valid file name for the model.')
    if '/' in base or '\x00' in base:
        raise SideloadError('Model file name must not contain path separators.')
    if not base.lower().endswith('.gguf'):
        raise SideloadError('The model file must be a .gguf file.')
    return base


def is_zip(path: str, declared_name: str = '') -> bool:
    """True when ``path`` is a real ZIP archive (magic-checked, not just by
    extension). ``declared_name`` is advisory only."""
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def extract_gguf_from_zip(zip_path: str, dest_dir: str) -> str:
    """Extract the single ``.gguf`` member of ``zip_path`` into ``dest_dir``,
    returning the extracted file path.

    Guards against zip-slip: the member is written to
    ``dest_dir/<sanitized-basename>`` — the archive's directory structure is
    discarded, so a ``../`` or absolute member can never escape ``dest_dir``.
    Raises :class:`SideloadError` when the archive is invalid, holds no
    ``.gguf``, or holds more than one.
    """
    if not zipfile.is_zipfile(zip_path):
        raise SideloadError('The uploaded file is not a valid .zip archive.')
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist()
                   if m.lower().endswith('.gguf') and not m.endswith('/')]
        if not members:
            raise SideloadError('The .zip archive contains no .gguf file.')
        if len(members) > 1:
            raise SideloadError(
                'The .zip archive contains more than one .gguf file — '
                'please upload a single .gguf (or a .zip with exactly one).')
        member = members[0]
        target = sanitize_gguf_filename(member)  # basename only → zip-slip-safe
        dest = os.path.join(dest_dir, target)
        with zf.open(member) as src, open(dest, 'wb') as out:
            shutil.copyfileobj(src, out, length=4 * 1024 * 1024)
    return dest


def find_ggufs_in_dir(dirpath: str) -> list:
    """Return the sorted list of ``.gguf`` file paths directly under ``dirpath``
    (non-recursive). Empty on error."""
    out = []
    try:
        for entry in sorted(os.listdir(dirpath)):
            p = os.path.join(dirpath, entry)
            if os.path.isfile(p) and entry.lower().endswith('.gguf'):
                out.append(p)
    except OSError:
        return []
    return out


def resolve_local_source(path: str, allowed_roots) -> str:
    """Resolve an operator-supplied local path (Input B) that the config
    container can read, bounded to ``allowed_roots``.

    Returns the real (symlink-resolved) absolute path. Raises
    :class:`SideloadError` when the path is empty, escapes every allowed root,
    or does not exist from the config container's view. Bounding to
    ``allowed_roots`` (default: the stack dir) is the guardrail that stops a
    sideload from pointing at arbitrary container-visible files (e.g. mounted
    secrets); the GGUF-magic check is the second gate.
    """
    if not path or not str(path).strip():
        raise SideloadError('No local path was given.')
    real = os.path.realpath(str(path).strip())
    roots = [os.path.realpath(r) for r in (allowed_roots or []) if r]
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise SideloadError(
            'The path is outside the allowed sideload directories. Stage the '
            'file under the stack directory (or a directory bind-mounted into '
            'razzfazz-config) and try again.')
    if not os.path.exists(real):
        raise SideloadError(
            'The path does not exist (as seen from the razzfazz-config '
            'container). For a USB / external mount it must be bind-mounted '
            'into razzfazz-config.')
    return real


def build_register_payload(name: str, filename: str, category: str) -> dict:
    """Build the GPUStack model-registration payload for a local GGUF.

    Uses :func:`model_source.model_source_fields` (offline=True) so the
    ``source: local_path`` spec — pointing at
    ``/var/lib/gpustack/local-models/<filename>`` with NO ``huggingface_*``
    keys — is produced by the same one-place-decides helper the install /
    reconcile paths use. ``restart_on_error`` is False (Strix kworker-storm
    guard, project_strix_halo_kworker_storm.md).
    """
    name = str(name or '').strip()
    if not name:
        raise SideloadError('A model name is required.')
    if category not in VALID_CATEGORIES:
        raise SideloadError(
            'Category must be one of: ' + ', '.join(VALID_CATEGORIES) + '.')
    base = sanitize_gguf_filename(filename)
    payload = {
        'name': name,
        'categories': [category],
        'replicas': 1,
        'restart_on_error': False,
    }
    # source: local_path + local_path=/var/lib/gpustack/local-models/<base>
    payload.update(model_source.model_source_fields(base, '', offline=True))
    return payload


def has_enough_space(available_bytes, file_size) -> bool:
    """True when ``available_bytes`` covers ``file_size`` plus the safety
    margin. Unknown free space (``None``) is treated as OK (fail-open — the
    write itself would still surface ENOSPC)."""
    if available_bytes is None:
        return True
    try:
        return int(available_bytes) >= int(file_size) + _FREE_SPACE_MARGIN_BYTES
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# gpustack-volume transfer helpers (docker exec via the socket-proxy)
# ---------------------------------------------------------------------------

def _docker(args, *, stdin=None, timeout=30, cwd=None):
    """Run a ``docker`` subcommand through the configured DOCKER_HOST (the
    socket-proxy). Returns the CompletedProcess."""
    return subprocess.run(
        ['docker', *args],
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )


def gpustack_available_bytes(container: str = GPUSTACK_CONTAINER):
    """Free bytes on the filesystem backing the gpustack-data volume, via
    ``docker exec gpustack df``. Returns an int, or None when it can't be
    determined (gpustack down, parse failure)."""
    try:
        r = _docker(['exec', container, 'sh', '-c',
                     f'df -PB1 {shlex.quote(GPUSTACK_VOLUME_ROOT)}'], timeout=15)
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    # POSIX df -P columns: Filesystem  <blocks>  Used  Available  Capacity  Mounted
    fields = lines[-1].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[3])
    except (ValueError, IndexError):
        return None


def target_exists(filename: str, container: str = GPUSTACK_CONTAINER) -> bool:
    """True when ``local-models/<filename>`` already exists in the volume."""
    dest = f'{LOCAL_MODELS_DIR}/{filename}'
    try:
        r = _docker(['exec', container, 'sh', '-c',
                     f'test -e {shlex.quote(dest)}'], timeout=15)
    except (subprocess.SubprocessError, OSError):
        return False
    return r.returncode == 0


def cleanup_stale_quarantine(container: str = GPUSTACK_CONTAINER):
    """Remove any orphaned ``.part`` quarantine files left by a prior aborted
    transfer (e.g. a worker killed mid-stream). Safe because the config
    container runs a single gunicorn worker — sideloads never overlap."""
    try:
        _docker(['exec', container, 'sh', '-c',
                 f'rm -f {shlex.quote(LOCAL_MODELS_DIR)}/.sideload-*.part'],
                timeout=15)
    except (subprocess.SubprocessError, OSError):
        pass


def _ensure_dir(container: str):
    r = _docker(['exec', container, 'sh', '-c',
                 f'mkdir -p {shlex.quote(LOCAL_MODELS_DIR)}'], timeout=15)
    if r.returncode != 0:
        raise SideloadError('Could not prepare the local-models directory in '
                            'the gpustack volume. Is the gpustack container '
                            'running?')


def _remote_size(path: str, container: str):
    try:
        r = _docker(['exec', container, 'sh', '-c',
                     f'wc -c < {shlex.quote(path)}'], timeout=30)
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except (ValueError, AttributeError):
        return None


def remove_remote(path: str, container: str = GPUSTACK_CONTAINER):
    try:
        _docker(['exec', container, 'sh', '-c',
                 f'rm -f {shlex.quote(path)}'], timeout=30)
    except (subprocess.SubprocessError, OSError):
        pass


def place_gguf(local_path: str, filename: str,
               container: str = GPUSTACK_CONTAINER, stream_timeout: int = 3600):
    """Stream a validated local GGUF into the gpustack volume and atomically
    place it at ``local-models/<filename>``.

    Streams to a hidden ``.part`` quarantine file first, size-verifies the
    transfer, then ``mv``'s it into place (atomic — same filesystem). On any
    failure the quarantine (and a partially-placed target) is removed so
    nothing half-written survives under ``local-models/``. Returns the
    in-container destination path on success.
    """
    _ensure_dir(container)
    quarantine = f'{LOCAL_MODELS_DIR}/.sideload-{secrets.token_hex(8)}.part'
    dest = f'{LOCAL_MODELS_DIR}/{filename}'
    try:
        local_size = os.path.getsize(local_path)
        with open(local_path, 'rb') as fh:
            r = _docker(['exec', '-i', container, 'sh', '-c',
                         f'cat > {shlex.quote(quarantine)}'],
                        stdin=fh, timeout=stream_timeout)
        if r.returncode != 0:
            raise SideloadError('Failed to write the GGUF into the gpustack '
                                'volume (docker exec returned '
                                f'{r.returncode}).')
        remote = _remote_size(quarantine, container)
        if remote is not None and remote != local_size:
            raise SideloadError('The GGUF transfer was truncated '
                                f'({remote} of {local_size} bytes) — aborted.')
        mv = _docker(['exec', container, 'sh', '-c',
                      f'mv {shlex.quote(quarantine)} {shlex.quote(dest)}'],
                     timeout=60)
        if mv.returncode != 0:
            raise SideloadError('Failed to move the GGUF into place in the '
                                'gpustack volume.')
    except SideloadError:
        remove_remote(quarantine, container)
        remove_remote(dest, container)
        raise
    except subprocess.TimeoutExpired:
        remove_remote(quarantine, container)
        raise SideloadError('The GGUF transfer timed out. For very large '
                            'models, stage the file on the box and use the '
                            '"local path" option instead of an upload.')
    except OSError as exc:
        remove_remote(quarantine, container)
        raise SideloadError(f'Could not read the staged GGUF: {exc}')
    return dest
