# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Shared `.env` parsing + writing utilities.

Mirrors `read_env_value` / `update_env_value` in scripts/lib.sh so the UI
containers and the bash management scripts agree on a single .env semantics:

- inline `<ws>#` comments are stripped (rc6.2 #2 fix)
- a value fully wrapped in matching `"..."` or `'...'` quotes is taken
  literally (so `#` inside the quotes is part of the value)
- URL colons / `#` fragments mid-value are preserved
- never `source` an operator-edited .env (rc6.x feedback rule); always
  parse explicitly

Consolidated from `core/config/app/services/env_utils.py` (which already
implemented the same parser). The S05 migration will replace that module
with a thin shim importing from here; for now both coexist.
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from pathlib import Path

# `${VAR}` and `${VAR:-default}` expansion. Mirrors POSIX shell parameter
# expansion semantics for the two forms .env files actually use:
#   - `${VAR}`           → value of VAR, empty string if unset
#   - `${VAR:-default}`  → value of VAR, `default` if unset OR empty
# Other forms (`${VAR-default}`, `${VAR:+x}`, indirect, substring, etc.)
# are NOT supported — keep parity with what razzfazz-init.sh + setup
# templates actually produce.
_VAR_EXPAND_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}')

# Quoted-value pattern: an entire value wrapped in matching `"..."` (or
# `'...'`) is treated as literal. `#` inside the quotes is part of the
# value, NOT the start of a comment. Trailing whitespace and an optional
# comment AFTER the closing quote are still trimmed.
_DOUBLE_QUOTED_RE = re.compile(r'^"(.*)"\s*(#.*)?$')
_SINGLE_QUOTED_RE = re.compile(r"^'(.*)'\s*(#.*)?$")

# Inline-comment trim: only `#` preceded by whitespace counts as a
# comment. Same convention as docker-compose / shell .env parsers. Bare
# `#` characters mid-value (e.g. URL fragments, hash tokens) are
# preserved.
_INLINE_COMMENT_RE = re.compile(r'\s+#.*$')


def parse_env_value(raw: str) -> str:
    """Normalise a single `.env` value string to its effective value.

    Mirrors `read_env_value` in scripts/lib.sh (rc6.2+).

      `17-0.107.0-ferretdb-2.7.0 # ferretdb-postgres` → `17-0.107.0-ferretdb-2.7.0`
      `"value with # not a comment"`                  → `value with # not a comment`
      `https://creators.dify.ai`                      → `https://creators.dify.ai`
      `  spaced  `                                    → `spaced`
    """
    if raw is None:
        return ''
    s = raw.strip()
    m = _DOUBLE_QUOTED_RE.match(s)
    if m:
        return m.group(1)
    m = _SINGLE_QUOTED_RE.match(s)
    if m:
        return m.group(1)
    # Unquoted: strip a trailing whitespace-prefixed `#` comment, then trim
    # surrounding whitespace and any naked enclosing quotes that the upgrade
    # script's `update_env_value` writer doesn't add but operator-edited
    # files sometimes carry.
    s = _INLINE_COMMENT_RE.sub('', raw)
    s = s.strip().strip('"').strip("'")
    return s


def _expand_vars(value: str, env: dict[str, str]) -> str:
    """Apply `${VAR}` and `${VAR:-default}` expansion using `env` as the
    source of values. Two-pass-stable: a value referencing a key whose
    own value also expands works as long as the dependency was parsed
    earlier in the same file (typical .env layout). Unknown vars in the
    bare `${VAR}` form expand to empty string — same as POSIX shell.
    """
    def _sub(m: re.Match) -> str:
        var = m.group(1)
        default = m.group(3) if m.group(2) else None
        if var in env and env[var]:
            return env[var]
        if default is not None:
            return default
        return ''
    return _VAR_EXPAND_RE.sub(_sub, value)


def expand_env_refs(value: str, env: dict[str, str]) -> str:
    """Expand `${VAR}` / `${VAR:-default}` in `value` against `env` ALONE.

    `read_env_file(expand=True)` merges the process environment over the
    file and lets it win — right for values that are handed to
    docker-compose, which resolves the same way. It is wrong for a caller
    that must report what THE FILE says: the UI containers carry some of
    the same keys as env vars, and where the two disagree the file is the
    box's authority (razzfazz-config reads MAIN_DOMAIN from the file for
    exactly that reason). This is the same expansion, with the caller
    choosing the dict.
    """
    return _expand_vars(value, env)


def read_env_file(path: Path | str, expand: bool = False) -> dict[str, str]:
    """Parse a `.env` file into a `{key: value}` dict.

    Skips blank and full-comment (`#…`) lines. Each value is normalised
    via `parse_env_value`. Returns an empty dict if the file doesn't
    exist (caller-side check is therefore optional).

    `expand` (#148): when True, `${VAR}` and `${VAR:-default}` references
    in values are expanded against earlier-parsed values + the process
    env. Off by default to preserve raw-string semantics for callers
    that want to forward values to docker-compose (which does its own
    expansion). Setup container's secrets-rotation flow uses expand=True
    so the value the operator sees in the UI matches the runtime value.
    """
    out: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip('\n').rstrip('\r')
                stripped = line.strip()
                if not stripped or stripped.startswith('#') or '=' not in stripped:
                    continue
                key, raw_value = line.split('=', 1)
                value = parse_env_value(raw_value)
                if expand:
                    # Merge process env so e.g. `${HOME}` resolves even
                    # when the .env file doesn't redefine it. Process env
                    # takes precedence over earlier .env entries to match
                    # docker-compose's resolution order.
                    scope = dict(out)
                    scope.update(os.environ)
                    value = _expand_vars(value, scope)
                out[key.strip()] = value
    except OSError:
        # Permission / I/O issues: return what we have. Caller decides
        # whether to surface the error or treat as empty config.
        pass
    return out


def read_env_key(path: Path | str, key: str, default: str = '') -> str:
    """Read a single `.env` key. Equivalent to `read_env_file(path).get(key, default)`
    but stops at the first match — cheap when the caller only needs one
    value (e.g. RAZZFAZZ_VERSION on every dashboard render).
    """
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith('#') or '=' not in stripped:
                    continue
                k, raw_value = line.split('=', 1)
                if k.strip() == key:
                    return parse_env_value(raw_value)
    except OSError:
        pass
    return default


def write_env_value(path: Path | str, key: str, value: str, atomic: bool = False) -> None:
    """Idempotently set $key=$value in $path. If $key exists, its line is
    replaced; otherwise the line is appended.

    Same semantics as `update_env_value` in scripts/lib.sh — operator-edited
    .env files keep their layout (no reordering, no comment stripping on
    untouched lines). Creates the file if it doesn't exist (the bash version
    returns 1 in that case; the Python version is more forgiving since
    callers who care can check `path.exists()` themselves).

    `atomic` (#148): when True, use fcntl LOCK_EX + write-to-tempfile +
    atomic rename. Required for callers doing live secrets rotation
    (setup container) where a partial write or concurrent reader would
    leave the .env in an inconsistent state. Off by default to keep the
    fast path cheap for code that just sets a one-off value.
    """
    p = Path(path)
    line_re = re.compile(rf'^{re.escape(key)}=')
    new_line = f'{key}={value}\n'

    if atomic:
        write_env_values_locked(p, {key: value})
        return

    if not p.exists():
        p.write_text(new_line)
        return

    with open(p) as f:
        lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line_re.match(line):
            lines[i] = new_line
            found = True
            break

    if not found:
        # Preserve trailing newline hygiene: if the file's last line lacks
        # a `\n`, add one before appending so the new key starts on its
        # own line.
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(new_line)

    with open(p, 'w') as f:
        f.writelines(lines)


def write_env_values_locked(path: Path | str, updates: dict[str, str]) -> None:
    """Apply N updates to `path` under fcntl LOCK_EX with atomic rename
    (#148). Equivalent to a sequence of `write_env_value(..., atomic=True)`
    calls but takes the lock once and writes once.

    Lock ordering: take exclusive lock on the .env file itself (or a
    sibling .lock file if the .env doesn't exist yet), then read+modify
    the buffer, write the buffer to a tempfile in the same directory,
    fsync, atomic rename over the .env. All writers using this helper
    serialize; any reader using a plain `open()` sees either the
    pre-update or post-update state, never a half-written line.

    Caveat: this does NOT cooperate with non-Python writers. The bash
    `update_env_value` in scripts/lib.sh uses sed in place (no fcntl).
    Within the razzfazz-config container all writes go through this helper
    so the lock holds; for cross-process safety with the bash side, avoid
    running `rzfz setup` (the host CLI in cli/setup_lib) while the config
    container is rotating secrets.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Take the lock on the file itself when present; otherwise a sibling
    # .lock file. fcntl locks are advisory but every caller in this code
    # path goes through this helper.
    lock_target = p if p.exists() else p.with_suffix(p.suffix + '.lock')
    lock_fd = os.open(str(lock_target), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            lines = p.read_text().splitlines(keepends=True) if p.exists() else []

            # Apply all updates against the buffer.
            for key, value in updates.items():
                line_re = re.compile(rf'^{re.escape(key)}=')
                new_line = f'{key}={value}\n'
                replaced = False
                for i, line in enumerate(lines):
                    if line_re.match(line):
                        lines[i] = new_line
                        replaced = True
                        break
                if not replaced:
                    if lines and not lines[-1].endswith('\n'):
                        lines[-1] += '\n'
                    lines.append(new_line)

            # Write to tempfile in the same directory + atomic rename.
            # NamedTemporaryFile + manual rename so we control the mode
            # (preserve the .env's existing perms) and don't get the
            # auto-delete-on-close behavior.
            fd, tmp_path = tempfile.mkstemp(
                prefix=p.name + '.', suffix='.tmp', dir=str(p.parent)
            )
            try:
                with os.fdopen(fd, 'w') as f:
                    f.writelines(lines)
                    f.flush()
                    os.fsync(f.fileno())
                # Preserve perms if .env already existed.
                if p.exists():
                    st = p.stat()
                    os.chmod(tmp_path, st.st_mode)
                os.replace(tmp_path, p)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
        # Clean up the sibling .lock file if we created one. Don't unlink
        # the .env itself.
        if lock_target != p and lock_target.exists() and lock_target.stat().st_size == 0:
            try:
                lock_target.unlink()
            except OSError:
                pass
