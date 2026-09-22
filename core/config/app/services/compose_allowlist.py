# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1226 — allow-list for the ``COMPOSE_FILE`` chain the Portal runs on.

**The hole.** BSB-03 mounts the repo root into ``razzfazz-config`` as ``:ro``
(ISO 27001 A.8.4), but a handful of narrow ``:rw`` islands live *inside* that
tree — ``.env`` first among them — and the Portal is allowed to drive
``docker compose up`` through the socket proxy (#1016). ``COMPOSE_FILE`` is
read by the compose CLI **out of that very ``.env``** and its entries are
resolved relative to the project directory. So the ``:ro`` root never had to be
defeated: anyone who reaches a Portal write path could drop a compose file into
a rw island (``backups/`` is the obvious one), point ``COMPOSE_FILE`` at it,
and have the daemon start a service definition of their choosing — arbitrary
bind mounts, ``privileged: true``, the host root. The read-only mount is an
A.8.4 claim about the *tree*; it says nothing about a file the Portal itself is
allowed to write.

**Operator decision E5 (2026-09-05, #979):** allow-list + guard. ``COMPOSE_FILE``
may only name repo-relative paths inside the ``:ro`` area; the Portal validates
before every compose run; a consistency guard pins the list. No ACCEPT-RESIDUAL.

**The rule.** An entry is accepted iff *all* of:

1. it is a plain relative path — no leading ``/`` or ``~``, no leading ``-``
   (which compose would read as an option); a URL is split by the ``:``
   separator and each half is refused on its own;
2. it contains no ``..`` segment and normalises to a path inside the repo root;
3. its basename follows the shipped convention ``compose*.yml`` / ``compose*.yaml``;
4. the file exists (a dangling entry is a loud refusal, matching
   ``scripts/lib.sh``'s "never wire a dangling COMPOSE_FILE reference");
5. it does **not** live under a path the Portal can write — :data:`RW_ISLANDS`.

Rule 5 is the security property; 1–4 keep the error messages honest and stop the
obvious escapes. The shape is deliberately *not* a literal file list: #448
documents that an operator may hand-add a module overlay and that a re-init has
to preserve it, so the allow-list describes a **location**, not an inventory.

**Not closed by this module** (see the PR text): ``modules/llm/mac-gateway`` is
rw-mounted as a *directory* and its ``compose.yml`` is pulled in by the root
``compose.yml``'s ``include:``, so it is loaded on every compose run regardless
of ``COMPOSE_FILE``. Narrowing that mount to the ``config.yaml`` the Mac panel
actually rewrites is a separate change against a separate write path.
"""
from __future__ import annotations

import os
import posixpath
import re

# ── the rw islands inside the :ro root ───────────────────────────────────────
# Repo-relative, ``/``-separated. Every entry corresponds 1:1 to a ``:rw`` bind
# of the razzfazz-config service in core/compose.yml — test_1226 derives that
# list from the compose file and fails if the two drift, so a new rw mount
# cannot silently widen the attack surface.
RW_ISLANDS = (
    '.env',
    '.env.dify',
    '.checksums.db',
    'backups',
    'certs',
    'config/manifests/versions.json',
    'modules/llm/mac-gateway',
    # #1223 (E4): the post-toggle probe queue. The Portal writes ONE request
    # JSON per toggle here and the host runner reads it — the narrowest seam
    # that lets a :ro-mounted Portal ask for something to be run. Named here
    # because #1505's rule is "every path the Portal can write is a path a
    # compose file could be staged in": without this entry
    # `COMPOSE_FILE=compose.yml:.probe-queue/compose.evil.yml` would be
    # ACCEPTED. The queue is not a compose location, and this is what says so.
    '.probe-queue',
)

# Compose files ship as compose.yml / compose.<variant>.yml / compose.devices.<hw>.yml.
_COMPOSE_NAME_RE = re.compile(r'\Acompose[A-Za-z0-9._-]*\.ya?ml\Z')

# Verbs that make the daemon ACT on the parsed model. Everything else
# (config/ps/images/--services/--profiles) only renders or reads.
STATE_CHANGING_VERBS = frozenset({
    'up', 'down', 'stop', 'start', 'restart', 'pull', 'run', 'create',
    'build', 'rm', 'kill', 'scale',
})

REMEDY = ("fix COMPOSE_FILE in .env (repo-relative compose*.yml paths inside the "
          "read-only stack root only), then retry — see #1226")


class ComposeFileRejected(RuntimeError):
    """A COMPOSE_FILE chain that the Portal refuses to run compose against."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__('refusing to run docker compose: ' + '; '.join(self.reasons)
                         + '. ' + REMEDY)


def _is_inside(rel_norm: str, island: str) -> bool:
    """Is the normalised repo-relative path at, or under, ``island``?"""
    return rel_norm == island or rel_norm.startswith(island + '/')


def check_entry(entry: str, stack_root: str) -> str | None:
    """Reason this single COMPOSE_FILE entry is rejected, or None if allowed."""
    if not entry or not entry.strip():
        return None  # empty segments are dropped by the caller, never rejected
    raw = entry.strip()

    if raw.startswith('-'):
        return f'{raw!r}: looks like a command-line option, not a path'
    if '\n' in raw or '\x00' in raw:
        return f'{raw!r}: contains a newline or NUL byte'
    if raw.startswith('~'):
        return f'{raw!r}: not a repo-relative path (home-relative)'
    if os.path.isabs(raw) or raw.startswith('/') or raw.startswith('\\'):
        return f'{raw!r}: not a repo-relative path (absolute)'

    unix = raw.replace('\\', '/')
    if any(seg == '..' for seg in unix.split('/')):
        return f'{raw!r}: contains a ".." segment'

    rel_norm = posixpath.normpath(unix)
    if rel_norm.startswith('../') or rel_norm == '..':
        return f'{raw!r}: escapes the stack root'
    if rel_norm.startswith('/'):
        return f'{raw!r}: escapes the stack root'

    name = posixpath.basename(rel_norm)
    if not _COMPOSE_NAME_RE.match(name):
        return (f'{raw!r}: not a compose file name '
                '(expected compose*.yml / compose*.yaml)')

    abs_path = os.path.join(stack_root, *rel_norm.split('/'))
    # A symlink inside the tree can point at a rw island (or out of the tree
    # entirely), so judge what the path actually resolves to as well.
    try:
        real = os.path.realpath(abs_path)
        real_root = os.path.realpath(stack_root)
    except OSError:  # pragma: no cover - realpath on a sane path does not raise
        return f'{raw!r}: cannot be resolved'
    if real != real_root and not real.startswith(real_root + os.sep):
        return f'{raw!r}: resolves outside the stack root ({real})'
    # THE security rule: the Portal must not be able to write the file it is
    # about to hand the daemon. Judged on the RESOLVED path, so a symlink
    # planted in the read-only tree cannot launder an island (or an outside
    # file) past the check. `realpath` normalises a non-existent path too, so
    # this covers the dangling case before the existence check below.
    real_rel = os.path.relpath(real, real_root).replace(os.sep, '/')
    for island in RW_ISLANDS:
        if _is_inside(real_rel, island):
            return (f'{raw!r}: lives under {island!r}, which the Portal can WRITE — '
                    'a compose file there is attacker-controllable (#1226)')

    if not os.path.isfile(abs_path):
        return f'{raw!r}: no such file under the stack root'
    return None


def split_chain(compose_file: str) -> list[str]:
    """The ``:``-joined chain as a list, empty segments dropped."""
    return [p for p in (compose_file or '').split(':') if p.strip()]


def check_compose_file(compose_file: str, stack_root: str) -> list[str]:
    """Every rejection reason for this chain (empty list = the chain is allowed).

    An EMPTY/unset chain is allowed: compose then uses its own discovery
    (``compose.yml`` in the project dir), which is in-tree and read-only.
    """
    return [r for r in (check_entry(e, stack_root) for e in split_chain(compose_file))
            if r]


def _read_env_compose_file(stack_root: str) -> str:
    """``COMPOSE_FILE`` as the compose CLI would read it from the project ``.env``.

    Deliberately a line scan, not a dotenv parse: this must agree with
    ``read_env_value`` in scripts/lib.sh and must never execute the file.
    """
    path = os.path.join(stack_root, '.env')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            value = ''
            for line in fh:
                line = line.strip()
                if line.startswith('COMPOSE_FILE='):
                    value = line.split('=', 1)[1].strip()
            return value.strip('"').strip("'")
    except OSError:
        return ''


def effective_compose_file(stack_root: str, env=None) -> str:
    """What compose will actually use: the environment wins over the ``.env``."""
    env = os.environ if env is None else env
    from_env = env.get('COMPOSE_FILE')
    if from_env is not None and from_env.strip():
        return from_env
    if from_env is not None and not from_env.strip():
        # An explicitly EMPTY override means "compose's own discovery"; the
        # .env value must not leak back in (build_preflight relies on this).
        return ''
    return _read_env_compose_file(stack_root)


def assert_compose_file_allowed(stack_root: str, env=None) -> None:
    """Raise :class:`ComposeFileRejected` when the effective chain is not allowed."""
    reasons = check_compose_file(effective_compose_file(stack_root, env), stack_root)
    if reasons:
        raise ComposeFileRejected(reasons)


def _verb(args) -> str:
    """First non-flag word of a compose argv tail (``--profile x up`` → ``up``)."""
    skip_value_for = {'--profile', '-p', '--project-name', '--project-directory',
                      '-f', '--file', '--env-file', '--parallel'}
    it = iter(args)
    for a in it:
        if not isinstance(a, str):
            continue
        if a in skip_value_for:
            next(it, None)
            continue
        if a.startswith('-'):
            continue
        return a
    return ''


def compose_argv(stack_root: str, *args, env=None, logger=None) -> list:
    """``['docker', 'compose', *args]`` — after validating the effective chain.

    THE chokepoint: every ``docker compose`` the Portal launches is built here,
    and test_1226 fails if a raw ``['docker', 'compose', …]`` literal reappears
    anywhere under ``core/config/app/``.

    A state-changing verb (:data:`STATE_CHANGING_VERBS`) on a rejected chain
    raises. A read-only verb only warns: refusing there would blank the
    dashboard of a box whose chain is merely odd, and rendering a config can
    start nothing.
    """
    reasons = check_compose_file(effective_compose_file(stack_root, env), stack_root)
    if reasons:
        verb = _verb(args)
        if verb in STATE_CHANGING_VERBS:
            raise ComposeFileRejected(reasons)
        if logger is not None:
            logger.warning('COMPOSE_FILE not allow-listed (#1226), running read-only '
                           '`docker compose %s` anyway: %s', verb or '?',
                           '; '.join(reasons))
    return ['docker', 'compose', *args]
