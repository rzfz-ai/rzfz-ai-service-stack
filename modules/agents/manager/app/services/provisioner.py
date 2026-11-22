# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Provisioning engine — creates and manages per-user agent instances."""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

import psycopg2

# #61 NEW-1: SINGLE source of truth for the user slug, shared with mcp-manager.
# Both services bind-mount core/common/razzfazz_common at /app/common and set
# PYTHONPATH=/app/common, so this import resolves identically in both. Re-exported
# here so existing `from app.services.provisioner import make_user_slug` callers
# (and the cross-service equality test) keep working. The two derivations MUST
# agree byte-for-byte or agent-manager's /internal/agent-wiring/<slug> lookup
# misses mcp-manager's instances and the per-proxy bearer never reaches the agent.
#
# #192: also re-export slug_candidates (current + legacy pre-hash slug) so
# ownership checks in proxy.py / api.py can accept instances provisioned
# before make_user_slug grew its hash suffix.
from razzfazz_common.user_slug import make_user_slug, slug_candidates  # noqa: F401
from razzfazz_common.rbac_guard import allows

from app.services import agent_updates
from app.services import sessions
from app.services.database import AdvisoryLockBusy
from app.services.names import NameRequired, NameTaken, name_key, suggest_name

logger = logging.getLogger(__name__)

# #36 security-review — the socket-less coding-agent types that run
# arbitrary/LLM-driven code and MUST be sandboxed (egress-controlled
# `coding-agents` net only, cap_drop ALL, read-only root + tmpfs, pids_limit,
# no host bind-mounts, no docker socket). Kept as an authoritative constant
# here (rather than a DB column) so enforcement is migration-free and cannot
# be weakened by a stale/partial DB row: a type is sandboxed iff it's in this
# set, regardless of what the DB carries. hermes/moltis/paperclip are
# deliberately NOT here — they keep their existing (unsandboxed) wiring.
SANDBOXED_TYPES = frozenset({'opencode', 'gsd-pi', 'codex', 'user-defined'})

#: #2554: a technical user's name (A17's NAME_RE, A1 D7; mcp-manager's
#: gateway_identity enforces the same rule and the reserved names).
TECHNICAL_NAME_RE = re.compile(r'^svc-[a-z0-9-]{3,40}$')



def scram_sha256_verifier(password: str, *, salt: bytes | None = None,
                          iterations: int = 4096) -> str:
    """#2586: the SCRAM-SHA-256 verifier PostgreSQL stores for a role password.

    `ALTER USER … PASSWORD '<plaintext>'` makes the server log the statement
    text, plaintext included, when `log_statement` is ddl/all or a duration
    threshold is hit. Sending the verifier instead means the only secret
    that can ever reach a server log is one that cannot be replayed as the
    password. Same construction as libpq's PQencryptPasswordConn (RFC 5802 /
    RFC 7677; psycopg2 does not expose it)."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    # SASLprep is the identity for the ASCII passwords generated here; a
    # non-ASCII password is NFKC-normalised, which is what PostgreSQL's
    # own pg_saslprep reduces to for the characters it permits.
    import unicodedata
    pw = unicodedata.normalize('NFKC', password).encode('utf-8')
    salted = hashlib.pbkdf2_hmac('sha256', pw, salt, iterations)
    client_key = hmac.new(salted, b'Client Key', hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b'Server Key', hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode('ascii')
    return f'SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}'

class StackHostPathUnset(RuntimeError):
    """A catalog bind mount names {{STACK_HOST_PATH}} but the manager has none."""


def _resolve_host_path(template: str) -> str:
    """#2831 — expand a catalog ``host_path`` bind source.

    ``{{STACK_HOST_PATH}}`` expanded to '' would hand the daemon ``/modules/...``:
    docker creates that as a root-owned EMPTY DIRECTORY on the host and the bind
    mount (a file) turns into a directory. Refuse instead; nothing is created.
    """
    if '{{STACK_HOST_PATH}}' not in template:
        return template
    base = (os.environ.get('STACK_HOST_PATH') or '').strip()
    if not base or not os.path.isabs(base):
        raise StackHostPathUnset(
            'STACK_HOST_PATH is unset, empty or not absolute in the agent manager; '
            'refusing to create a bind mount from a made-up host path '
            '(set STACK_HOST_PATH in .env, recreate agent-manager)')
    return template.replace('{{STACK_HOST_PATH}}', base.rstrip('/') or '/')


def _is_technical_config(instance_config) -> bool:
    """#2554: the instance runs as a technical user (openbao_tokens.is_technical's rule)."""
    cfg = instance_config if isinstance(instance_config, dict) else {}
    return cfg.get('identity_mode') == 'technical' or bool(cfg.get('technical_user') or cfg.get('_technical_user'))


def _technical_user_of(instance_config) -> str:
    """#2554: the valid svc- name a technical instance runs as, or ''."""
    cfg = instance_config if isinstance(instance_config, dict) else {}
    name = cfg.get('_technical_user') or cfg.get('technical_user') or ''
    return name if isinstance(name, str) and TECHNICAL_NAME_RE.fullmatch(name) else ''


def runs_as(instance) -> str:
    """#2554: the technical user an instance runs as ('' for a personal one),
    for the admin page. Read from the agent manager's own instance record."""
    cfg = (instance or {}).get('config') or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    if not _is_technical_config(cfg):
        return ''
    return _technical_user_of(cfg) or '(invalid technical user)'


def _identity_mode(user_config) -> str:
    """#2554: 'personal' (default) or 'technical'. Any other value is
    refused (PermissionError), never read as personal."""
    mode = (user_config or {}).get('identity_mode', 'personal')
    if mode in (None, '', 'personal'):
        return 'personal'
    if mode == 'technical':
        return 'technical'
    raise PermissionError('identity_mode must be personal or technical')

# W4a (#256) — socket-less assistant classes fenced onto the egress-controlled
# `agent-assistants` net (gpustack + agent-manager join it; postgres / valkey /
# authentik / config-portal stay unreachable). Deliberately NOT here:
#   * paperclip — needs postgres+valkey directly (per-instance DB creds bound
#     the blast radius instead).
# #2661: moltis lost the docker socket (it only fed its optional exec sandbox),
# so the reason for its exemption is gone and it joins the fence like hermes.
# Same migration-free authority pattern as SANDBOXED_TYPES.
ASSISTANT_NET_TYPES = frozenset({'hermes', 'moltis'})

#: The agent types the stack no longer ships (2026.11: OpenHands). Their
#: catalog row stays, disabled and deprecated (schema migration 16), so an
#: instance launched before the upgrade is still listed to its owner and can be
#: deleted; its data volume is kept until then. It never starts or recreates
#: again: its image and the runtime it needed are gone from the stack.
REMOVED_TYPES = frozenset({'openhands'})


def removed_type_refusal(agent_type: str) -> str | None:
    """The user-facing refusal for an instance of a removed type, else None."""
    if agent_type not in REMOVED_TYPES:
        return None
    return ('This agent type is no longer part of the stack, so the agent cannot '
            'be started or reinstalled. Its files stay in place until you delete it.')

# #256 residual — `coding-tools` is socket-less and db-less like the split
# coding types, but predates them: #36 SPLIT it into opencode / gsd-pi / codex /
# user-defined and left it `enabled: False`, so it never entered
# SANDBOXED_TYPES. A disabled type is not a dead one — an instance provisioned
# BEFORE the split still resolves its type_info, and every start recreates the
# container. It therefore kept landing on `razzfazz-stack_default`, flat with
# postgres / valkey / authentik / config-portal, on exactly the boxes that have
# been upgraded the longest.
#
# It gets the NET fence only, not `sandbox=True`: the hostile-container profile
# (read-only rootfs, cap_drop ALL, tmpfs /tmp, docker-init as PID 1) was
# designed against the SPLIT images. Applying it to a pre-split container whose
# user has months of state in it would be a behaviour change smuggled in under
# a security fix. The `coding-agents` whitelist already carries everything its
# catalog entry names (gpustack, gitea, cognee, docling, searxng, …), so the
# fence costs it nothing.
CODING_NET_TYPES = frozenset({'coding-tools'})

# #36 / PR #84 — agent memory governance.
# Per-instance memory presets offered in the UI (GB). The default is the floor
# every user gets; power/admin may raise up to the per-instance max.
MEM_PRESETS_GB = (2, 4, 8, 16)
# Only a power/admin tier may set a custom per-instance memory. We derive
# "power/admin" from the EXISTING tier model (resolve_user_tier → priority):
# agent-basic=0, agent-power=10, agent-admin=99, unlimited-admin=1000. Anyone at
# or above this threshold is a power/admin; a regular (basic) user is below it
# and always gets the default, server-side (the UI just hides the field).
POWER_TIER_MIN_PRIORITY = 10


#: An env_template placeholder, `{{name}}` (#2554 review: one-pass resolve).
_PLACEHOLDER = re.compile(r'\{\{([A-Za-z0-9_]+)\}\}')


# ── Gitea checkout wiring (#165) ─────────────────────────────────────────────
def _gitea_internal_url() -> str:
    """The Gitea endpoint reachable from inside the sandbox (agent net).
    Defaults to the docker-DNS name every stack service uses."""
    return (os.environ.get('GITEA_INTERNAL_URL', '') or '').strip() or 'http://gitea:3000'


def _gitea_external_url() -> str:
    """The public Gitea base URL the web UI shows (and the entrypoint rewrites
    to the internal host). Prefer an explicit GITEA_EXTERNAL_URL; else derive
    from GITEA_DOMAIN (default `git.<MAIN_DOMAIN>`, matching .env.example). Guard
    against an unexpanded `${MAIN_DOMAIN}` literal (env_file doesn't expand it)."""
    ext = (os.environ.get('GITEA_EXTERNAL_URL', '') or '').strip()
    if ext:
        return ext
    main_domain = os.environ.get('MAIN_DOMAIN', 'localhost')
    gdom = (os.environ.get('GITEA_DOMAIN', '') or '').strip()
    if not gdom or '${' in gdom:
        gdom = f'git.{main_domain}'
    return f'https://{gdom}'


def _tier_allows_custom_memory(tier: dict | None) -> bool:
    """Server-side tier gate for the per-instance memory control. True only for
    power/admin tiers (priority >= POWER_TIER_MIN_PRIORITY)."""
    if not tier:
        return False
    try:
        return int(tier.get('priority') or 0) >= POWER_TIER_MIN_PRIORITY
    except (TypeError, ValueError):
        return False


# --- #233: per-instance name -------------------------------------------------
INSTANCE_NAME_MAX = 64


def validate_custom_name(raw) -> tuple[str | None, str | None]:
    """Validate a user-chosen instance name. Returns (name, error).

    `name` is '' for "clear the override" (fall back to the type's display
    name); a non-None `error` means refuse.

    This is NOT cosmetic validation. The name is injected into the container as
    `AGENT_LABEL` and rendered into the terminal's title bar — and a terminal
    title is set with an OSC escape sequence. A name carrying raw control bytes
    is therefore an injection vector, not a typo. It also lands in a JSONB
    column, a JSON API response and several HTML pages.

    So: reject every character in Unicode's C* categories. That covers the
    obvious control bytes (ESC, NUL, BEL, newline) AND the format characters —
    RLO/LRO bidi overrides, zero-width joiners — that let a name render as
    something other than what it is. Everything a human actually wants in a name
    (letters in any script, digits, spaces, punctuation, emoji) is in another
    category and passes untouched.
    """
    if raw is None:
        return '', None
    if not isinstance(raw, str):
        return None, 'Name must be text.'
    name = raw.strip()
    if not name:
        return '', None
    if len(name) > INSTANCE_NAME_MAX:
        return None, (f'Name is too long — {len(name)} characters, '
                      f'maximum {INSTANCE_NAME_MAX}.')
    import unicodedata
    for ch in name:
        if unicodedata.category(ch)[0] == 'C':
            return None, ('Name contains a control or formatting character. '
                          'It is shown in a terminal title bar, where those '
                          'are interpreted rather than displayed.')
    return name, None


def instance_suffix(instance_no) -> str:
    """#1988: what distinguishes instance #N of a type in every docker-side
    name. Empty for #1 - so every pre-#1988 row keeps the container, volume
    and database names it already has - and `-N` from the second on."""
    try:
        n = int(instance_no or 1)
    except (TypeError, ValueError):
        n = 1
    return '' if n <= 1 else f'-{n}'


def instance_db_identity(type_id: str, user_slug: str, instance_no=1) -> tuple[str, str]:
    """#2529: `(db_name, db_user)` of a per-instance database by the #1988
    formula. Only for a row that carries no persisted names yet; a row that
    does is read with `db_identity_of`, never recomputed."""
    sfx = instance_suffix(instance_no).replace('-', '_')
    base = f"agent_{type_id}_{user_slug}{sfx}"
    return f"{base}_db", base


def db_identity_of(instance_config: dict | None, type_id: str, user_slug: str,
                   instance_no=1) -> tuple[str, str]:
    """#2529: the names this instance's database was CREATED with — the
    persisted `_db_name`/`_db_user`; the formula only for a row the start-up
    back-fill (services/instance_db.py) has not reached."""
    cfg = instance_config or {}
    if cfg.get('_db_name') and cfg.get('_db_user'):
        return cfg['_db_name'], cfg['_db_user']
    return instance_db_identity(type_id, user_slug, instance_no)


def owner_slug_of(instance: dict, instance_config: dict | None) -> str:
    """#2529: the slug the owner's per-user services key on (their MCP wiring):
    the recorded canonical `_owner_slug`, else the row's own slug."""
    return (instance_config or {}).get('_owner_slug') or instance.get('user_slug') or ''


def key_owner_of(instance: dict, instance_config: dict | None,
                 caller: str | None, caller_uid: str | None) -> tuple[str | None, bool]:
    """#1039: whom a missing llm-manager key is minted for, and whether that
    name should be recorded on the row: ``(username | None, record)``.

    The recorded `_owner_username` when there is one. Otherwise the caller —
    but only when the caller is the row's AUTHENTICATED owner (their
    forward-auth uid equals the row's `user_id`), and then it is recorded so
    later recreates by anyone else find it. Otherwise None: the mint is
    deferred, never done for the caller."""
    recorded = (instance_config or {}).get('_owner_username')
    if recorded:
        return recorded, False
    row_uid = str(instance.get('user_id') or '').strip()
    if caller and caller_uid and row_uid and str(caller_uid).strip() == row_uid:
        return caller, True
    return None, False


def db_identity_from_env(env: dict | None) -> tuple[str, str] | None:
    """#2529: `(db_name, db_user)` a container actually connects with, read
    from its DATABASE_URL; None when there is none or it does not parse."""
    raw = (env or {}).get('DATABASE_URL') or ''
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    name = (parts.path or '').lstrip('/')
    if parts.scheme not in ('postgres', 'postgresql') or not parts.username or not name:
        return None
    return name, unquote(parts.username)


def _instance_mem_limit(instance: dict, type_info: dict | None) -> str:
    """The mem_limit an existing instance runs with: the per-instance choice
    persisted on its config (#36), else its catalog type's default — the
    same resolution Database._row_mem_mb applies to the running sums."""
    cfg = _parse_config(instance.get('config'))
    return cfg.get('mem_limit') or (type_info or {}).get('mem_limit') or '0'


def _parse_config(cfg) -> dict:
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def db_password_from_env(env: dict | None, db_user: str) -> str | None:
    """#1039: the password a container connects to Postgres with, read from
    its DATABASE_URL — only when it connects as `db_user`; None otherwise
    (no env, no password, or another role: before #2529 instance #N connected
    as instance #1's role, with #1's password). The raw URL text is returned:
    `_resolve_env` renders `{{instance_db_password}}` unencoded, so the value
    persisted must be exactly what the URL carries (see ensure_db_password,
    which refuses one that would need encoding)."""
    raw = (env or {}).get('DATABASE_URL') or ''
    try:
        parts = urlsplit(raw)
        user, password = parts.username, parts.password
    except ValueError:
        return None
    if not user or unquote(user) != db_user or not password:
        return None
    return password


_URL_SAFE_PASSWORD = re.compile(r'[A-Za-z0-9._~-]+')


# ── E1 (#2518): volumes bind to what the containers ACTUALLY mounted ─────────
# Recreate and delete used to RECOMPUTE every volume name as
# `agent-{type}-{row.user_slug}{sfx}-{name_suffix}` from the CURRENT catalog.
# Two things break that on a live box:
#   * the row's slug changes (#192's legacy-slug forward migration) while the
#     volumes keep the name they were created with — prod 2026-09-26: the
#     operator's opencode was recreated onto a fresh, EMPTY set;
#   * the catalog renames a volume (moltis `-config`/`-data` →
#     `home-config-moltis`/`home-moltis`) — delete then misses the real ones.
# The names now come from (in order) the live containers' Mounts, the names
# persisted on the row (`mounted_volumes`, migration 14), the pattern derived
# from the row's `container_name` (which never changes, unlike user_slug), and
# otherwise recreate REFUSES instead of creating empties.

def upgrade_succeeded(iid, msg) -> bool:
    """THE success predicate for an `(instance_id, message)` outcome of
    `Provisioner.upgrade()` (and of `start()`, which delegates its self-heal
    to it). #2518 fix round 2: one definition shared by the API routes,
    repair, update_memory, volume_migrator and post-install's re-key, so they
    cannot drift. `upgrade()` signals failure either with a None id (not
    found, recreate refused, aborted before any change) or with a message
    containing "failed" (catalog type missing, remove failed, recreate failed).
    """
    return bool(iid) and 'failed' not in (msg or '').lower()


class VolumeBindingRefused(Exception):
    """Recreate cannot find the volumes an existing instance used. Raised
    instead of silently mounting a fresh, empty set."""


def named_volume_specs(type_info: dict | None, field: str = 'volumes') -> list:
    """The catalog's NAMED volume specs for `field` (host_path binds excluded)."""
    if not type_info:
        return []
    raw = type_info.get(field) or '[]'
    try:
        specs = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return [v for v in (specs or [])
            if isinstance(v, dict) and 'name_suffix' in v and 'host_path' not in v]


def expected_volume_map(container_name: str, specs: list) -> dict:
    """{name: mount} as launch names them: `<container_name>-<name_suffix>`.
    Launch builds both from the SAME `agent-{type}-{slug}{sfx}` stem, so the
    stored container_name reproduces the volume names even after the row's
    user_slug moved to the canonical slug."""
    return {f"{container_name}-{v['name_suffix']}": v.get('mount') for v in specs}


def normalise_mounted_volumes(raw) -> dict | None:
    """The row's `mounted_volumes` as {"primary": {...}, "companion": {...}},
    or None when nothing was recorded (NULL / unreadable / both empty)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    out = {}
    for role in ('primary', 'companion'):
        m = raw.get(role) or {}
        out[role] = ({str(k): str(v) for k, v in m.items() if k and v}
                     if isinstance(m, dict) else {})
    return out if (out['primary'] or out['companion']) else None


#: docker's anonymous-volume names: 64 hex chars. A missing anonymous volume
#: was pruned with its container; the image re-creates it, so it never blocks
#: a recreate the way a missing NAMED data volume does.
_ANON_VOLUME_RE = re.compile(r'^[0-9a-f]{64}$')


def _mounts_of(brief) -> dict | None:
    m = brief.get('mounts') if isinstance(brief, dict) else None
    return m if isinstance(m, dict) else None


def instance_containers(docker_client, instance: dict, companion_name: str | None) -> list[dict]:
    """E1 (#2518): an instance's containers as docker has them — every
    container labelled `razzfazz.agent.instance=<id>` (primary + companion),
    plus the exact names (container_name, companion_name) for containers that
    predate the label. A name match labelled with a DIFFERENT instance is never
    ours. Each entry: {name, companion, mounts, ...}. Docker errors RAISE —
    "cannot tell" must never read as "mounts nothing". Shared by the
    provisioner and the back-fill (services/slug_migration.py)."""
    found = {}
    for b in (docker_client.find_instance_containers(instance['id']) or []):
        if isinstance(b, dict) and b.get('name'):
            found[b['name']] = b
    iid = str(instance['id'])
    for name in filter(None, (instance.get('container_name'), companion_name)):
        if name in found:
            continue
        b = docker_client.inspect_container_brief(name)
        if not isinstance(b, dict):
            continue
        owner = (b.get('labels') or {}).get('razzfazz.agent.instance')
        if owner and owner != iid:
            logger.warning(
                "container %s is labelled for instance %s, not %s — "
                "not treating it as this instance's (#2518)", name, owner, iid)
            continue
        found[name] = b
    out = []
    for name, b in found.items():
        b = dict(b)
        b['companion'] = bool(b.get('companion')) or name != instance.get('container_name')
        out.append(b)
    return out


def instance_display_name(instance: dict, type_info: dict | None = None) -> str:
    """The name to SHOW for an instance: the user's own, else the type's.

    One helper because #233's whole point is that the name is the same on every
    surface — manager cards, the portal tree, start-portal tiles. A second
    derivation somewhere is how they drift apart.
    """
    cfg = (instance or {}).get('config') or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            cfg = {}
    if cfg.get('custom_name'):
        return cfg['custom_name']
    base = ((type_info or {}).get('display_name')
            or (instance or {}).get('type_display_name')
            or (instance or {}).get('agent_type')
            or '')
    # #1988: a second instance of a type without a name of its own says which
    # one it is; #1 keeps the bare type name every surface showed before.
    sfx = instance_suffix((instance or {}).get('instance_no'))
    return f"{base} #{sfx[1:]}" if (base and sfx) else base


# --- #232: per-instance PID cap ----------------------------------------------
# Presets offered in the UI. 512 is docker_client's floor for types that set
# nothing; 2048 is where ga.9 (#221) put the coding family after MCP-heavy
# workloads exhausted 512.
PIDS_PRESETS = (512, 1024, 2048, 4096, 8192)
PIDS_MIN = 64        # below this a shell plus its children cannot start
PIDS_MAX = 16384     # a cap this high stops being a cap; a fork bomb is a
                     # whole-box event, so the ceiling is enforced server-side


def validate_pids_limit(raw) -> tuple[int | None, str | None]:
    """Validate a requested PID cap. Returns (value, error)."""
    if isinstance(raw, bool) or raw is None:
        return None, 'A PID limit is required.'
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, 'PID limit must be a whole number.'
    if value < PIDS_MIN or value > PIDS_MAX:
        return None, (f'PID limit must be between {PIDS_MIN} and {PIDS_MAX}.')
    return value, None


def resolve_pids_limit(instance_config: dict | None,
                       type_info: dict | None) -> int | None:
    """The PID cap to apply: the instance's own, else the agent type's default.

    ga.9 (#221) made `pids_limit` a per-TYPE column. That is the right default
    but the wrong unit of control — the cap is exhausted by one user's
    MCP-heavy sandbox, not by the type. This makes the type value a default
    that a per-instance override beats. `None` means "let docker_client apply
    its own floor", which is what an unconfigured type has always done.

    AGM-2: the resolved value is CLAMPED to PIDS_MIN..PIDS_MAX regardless of
    where it came from. `validate_pids_limit` guards the /api/pids edge, but a
    config row can also be written by /api/launch (and by a pre-clamp
    instance), and an out-of-bounds value reaching docker means either a
    container that cannot fork a shell (below the floor) or — for `-1`, which
    docker reads as UNLIMITED — a sandboxed agent that can fork-bomb the box.
    A cap that the value's own source can switch off is not a cap, so the
    resolver enforces the bounds itself rather than trusting its inputs.
    """
    cfg = instance_config or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            cfg = {}
    for source in (cfg, type_info or {}):
        raw = source.get('pids_limit')
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        return max(PIDS_MIN, min(value, PIDS_MAX))
    return None


# --- AGM-2: the /api/launch body allow-list ----------------------------------
# The ONLY keys a launch request may set. Everything else the provisioner
# stores on `instance_config` is its own state — the minted secrets
# (`_generated_secret`, `_gitea_token`, `_db_password`, `_llm_manager_key`,
# `_llm_manager_key_id`), the minted DB names (`_db_name`, `_db_user`, #2529)
# and the server-resolved `mem_limit`.
LAUNCH_CONFIG_ALLOWED_KEYS = frozenset({'mem_gb', 'custom_name', 'pids_limit', 'new_instance',
                                        'identity_mode', 'technical_user'})


def sanitize_launch_config(raw, tier: dict | None = None) -> tuple[dict | None, str | None]:
    """AGM-2 — reduce a raw /api/launch body to what a user may actually set.

    Returns (config, error); a non-None error means refuse the launch.

    `POST /api/launch/<type>` used to hand its request JSON straight to
    `Provisioner.launch`, which wrote it verbatim into `instance_config`. Three
    consequences, all of them reachable by any authenticated basic-tier user:

      * `pids_limit` bypassed BOTH the tier gate and the PIDS_MIN..PIDS_MAX
        bounds that `/api/pids` enforces — `-1` means "unlimited PIDs" to
        docker, i.e. a fork bomb inside an otherwise-sandboxed agent;
      * `custom_name` bypassed `validate_custom_name`, which exists because the
        name is injected as `AGENT_LABEL` and rendered into a terminal title
        bar with an OSC escape sequence;
      * `_llm_manager_key_id` let a launch pre-seed ANOTHER user's llm-manager
        key id, so deleting your own agent revoked their key (a cross-user
        IDOR through delete's `_revoke_llm_manager_key`).

    So the edge filters to an allow-list instead of blacklisting the known-bad
    keys: an unknown knob is silently dropped (a client bug, not something the
    user needs a message about), while a `_`-prefixed key is REFUSED outright —
    a request carrying one is trying to write provisioner state and deserves to
    fail loudly rather than quietly.

    `mem_gb` needs no check here: `resolve_mem_limit` is already the
    server-side tier gate + clamp for it, and it ignores an unparsable value.
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, 'Launch configuration must be a JSON object.'

    for key in raw:
        if isinstance(key, str) and key.startswith('_'):
            return None, (f'Unsupported launch option “{key}”. Keys starting '
                          f'with “_” are internal to the provisioner.')

    unknown = sorted(k for k in raw if k not in LAUNCH_CONFIG_ALLOWED_KEYS)
    if unknown:
        logger.info("launch: dropping unsupported config key(s) %s", unknown)

    cfg: dict = {}
    if 'mem_gb' in raw:
        cfg['mem_gb'] = raw['mem_gb']

    if 'custom_name' in raw:
        name, error = validate_custom_name(raw['custom_name'])
        if error:
            return None, error
        if name:
            cfg['custom_name'] = name

    # #1988: `new_instance: true` asks for ANOTHER instance of a type the user
    # already has; anything but the boolean true is dropped, so a string
    # "false" cannot pass for a request.
    if raw.get('new_instance') is True:
        cfg['new_instance'] = True
    if 'pids_limit' in raw:
        if not _tier_allows_custom_memory(tier):
            return None, ('Your tier does not permit setting the PID limit. '
                          'Ask an admin to raise it.')
        value, error = validate_pids_limit(raw['pids_limit'])
        if error:
            return None, error
        cfg['pids_limit'] = value

    # #2554: personal (default) or technical git identity. Only the SHAPE is
    # checked here; who may use which technical user is api._authorize_identity
    # (and, again, Provisioner._gitea_credentials). `technical_user` without
    # `identity_mode: technical` is dropped.
    if 'identity_mode' in raw:
        mode = raw['identity_mode']
        if not isinstance(mode, str) or mode not in ('personal', 'technical'):
            return None, 'Identity must be "personal" or "technical".'
        cfg['identity_mode'] = mode
        if mode == 'technical':
            name = raw.get('technical_user')
            if not isinstance(name, str) or not TECHNICAL_NAME_RE.fullmatch(name):
                return None, 'A technical launch needs a technical user (svc-…).'
            cfg['technical_user'] = name

    return cfg, None


# #959 D1: widened from #612's OPENAI_*/GPUSTACK_* pair to also cover the
# LLM_BASE_URL/LLM_EMBEDDING_BASE_URL (+ their *_API_KEY) agents whose
# catalog env never went through the OpenAI/GPUStack-shaped names.
_LLM_BASE_ENV_KEYS = ('OPENAI_BASE_URL', 'GPUSTACK_BASE_URL', 'LLM_BASE_URL', 'LLM_EMBEDDING_BASE_URL',
                      'MOLTIS_PROVIDERS__OPENAI__BASE_URL')
_LLM_KEY_ENV_KEYS = ('OPENAI_API_KEY', 'GPUSTACK_API_KEY', 'LLM_API_KEY', 'LLM_EMBEDDING_API_KEY')

# #612/#959 — the LLM Manager's OpenAI-compatible ingress. Single source of
# truth for both the env-var rewrite (_apply_llm_manager_endpoint) and the
# command-placeholder rewrite (_resolve_command_llm_placeholders, #959 D2)
# so the two switches can never drift apart.
# #1445 (cutover C5b / operator decision E1): the CANONICAL, backend-invariant
# endpoint. `llm` is a network alias of the manager on every agent net (#1443),
# so an agent's env no longer names a backend at all — the manager fronts
# GPUStack (#1442) or its own workers. The old container name stays known
# below, so instances persisted before the cutover are repointed too.
_LLM_MANAGER_BASE_URL = 'http://llm:8080/v1'

# Endpoints a persisted instance may still carry from before #1445; every one
# of them is rewritten to the canonical URL when a manager key is minted.
_LEGACY_LLM_BASE_URLS = (
    'http://gpustack:9090/v1-openai',
    'http://llm-manager:8080/v1',
)


_GPUSTACK_DEFAULT_BASE_URL = 'http://gpustack:9090/v1-openai'

# AGM-9 (#1039) — the shape a minted llm-manager key must have before it may be
# substituted anywhere. The resolved `command` list is handed to `docker create`
# as the container CMD and at least one catalog entry (hermes) embeds
# {{LLM_API_KEY}} INSIDE an `sh -c` script, so a key carrying a quote, `$`,
# backtick or whitespace would be shell metacharacters in that word. Today's
# keys are `rzfz-sk…`-shaped and there is no live injection — this is the guard
# that keeps the documented extension point (new catalog commands using the same
# placeholders) from becoming one. Fails CLOSED: a malformed key is dropped, so
# the caller takes the existing "no key" branch (catalog gpustack default),
# which is a safe, already-tested state.
_LLM_KEY_SAFE_RE = re.compile(r'\A[A-Za-z0-9_.\-]+\Z')


def _safe_llm_key(llm_key: str | None) -> str | None:
    """Return `llm_key` iff it is safe to substitute, else None (fail closed)."""
    if not llm_key:
        return None
    if not isinstance(llm_key, str) or not _LLM_KEY_SAFE_RE.match(llm_key):
        logger.warning(
            "llm-manager key rejected: not [A-Za-z0-9_.-]+ (len=%d) — falling "
            "back to the catalog default endpoint", len(llm_key or ''))
        return None
    return llm_key


# AGM-9 — the env keys that carry a whole LLM config BLOB (an opaque string with
# the endpoint and key baked inside), which the named-key rewrite below cannot
# reach. See llm_config.opencode_config_json / gsd_models_json.
_LLM_BLOB_ENV_KEYS = ('OPENCODE_CONFIG_JSON', 'PI_MODELS_JSON',
                      'GSD_MODELS_JSON', 'OPENCODE_GPUSTACK_CONFIG')


def _apply_llm_manager_endpoint(env: dict, llm_key: str | None) -> None:
    """#612/#959 — repoint an instance's model-endpoint env at the LLM Manager.

    NO-OP when `llm_key` is falsy: this is a gpustack-only box (no key was
    minted) and the catalog defaults must survive byte-for-byte. When a key
    WAS minted (an LLM-Manager box), every present base-URL key is repointed
    at the manager's OpenAI ingress and every present key-env is set to the
    minted key; OPENAI_API_KEY is always set, as #612 did.
    """
    # AGM-9: an unparseable key is treated as no key at all (fail closed).
    llm_key = _safe_llm_key(llm_key)
    if not llm_key:
        return
    for k in _LLM_BASE_ENV_KEYS:
        if k in env:
            env[k] = _LLM_MANAGER_BASE_URL
    for k in _LLM_KEY_ENV_KEYS:
        if k in env:
            env[k] = llm_key
    env['OPENAI_API_KEY'] = llm_key

    # #959 D3 (integration-test finding): opencode's OPENCODE_CONFIG_JSON and
    # user-defined's PI_MODELS_JSON aren't plain endpoint/key env vars — they
    # are pre-baked JSON blobs (llm_config.opencode_config_json /
    # gsd_models_json) that embed the box's raw gpustack base URL AND its
    # literal GPUSTACK_API_KEY value as a SUBSTRING of an otherwise-opaque
    # config string. The named-key rewrite above can never reach a value
    # baked inside another value, so an LLM-Manager box was shipping these
    # two agents a still-live gpustack credential. Swap the substring so no
    # catalog-baked blob can carry a stale gpustack endpoint/key past this
    # switch.
    #
    # AGM-9 (#1039): the ENDPOINT swap is safe to run over every string value —
    # `http://gpustack:9090/v1-openai` is unambiguous and repointing it is
    # always the intent. The KEY swap is not: `os.environ['GPUSTACK_API_KEY']`
    # is an arbitrary operator-set string, and a plain `str.replace` over the
    # whole dict silently rewrites any unrelated value that happens to contain
    # that byte sequence (a short or dictionary-word key makes that likely, not
    # theoretical). So the key swap is scoped to the values that actually carry
    # LLM config: a named key/blob env var, or a value that was carrying the
    # gpustack endpoint itself — which is how llm_config bakes the two, always
    # together. Anything else keeps its bytes.
    gpustack_key = os.environ.get('GPUSTACK_API_KEY', '')
    for k, v in env.items():
        if not isinstance(v, str):
            continue
        carries_llm_config = (
            any(_legacy in v for _legacy in _LEGACY_LLM_BASE_URLS)
            or _LLM_MANAGER_BASE_URL in v
            or k in _LLM_BLOB_ENV_KEYS
            or k in _LLM_KEY_ENV_KEYS
        )
        new_v = v
        for _legacy in _LEGACY_LLM_BASE_URLS:
            new_v = new_v.replace(_legacy, _LLM_MANAGER_BASE_URL)
        if gpustack_key and carries_llm_config:
            new_v = new_v.replace(gpustack_key, llm_key)
        if k in _LLM_BLOB_ENV_KEYS:
            new_v = _blob_with_manager_key(new_v, llm_key)
        if new_v != v:
            env[k] = new_v


def _blob_with_manager_key(raw: str, llm_key: str) -> str:
    """Set the key of every provider block in a config blob that points at the
    LLM Manager to `llm_key`.

    The substring swap above only works while GPUSTACK_API_KEY is set: on a box
    without it the blob was baked with an EMPTY apiKey, there is nothing to
    swap, and the agent (opencode, gsd, Pi) called the manager without a key
    and got 401. A provider block is recognised by its base URL (`baseURL` in
    opencode's options, `baseUrl` in gsd's models.json); a block pointing
    anywhere else keeps its key. Unparseable input keeps its bytes."""
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    changed = False

    def _walk(node):
        nonlocal changed
        if isinstance(node, dict):
            base = node.get('baseURL', node.get('baseUrl'))
            if (base == _LLM_MANAGER_BASE_URL and 'apiKey' in node
                    and node['apiKey'] != llm_key):
                node['apiKey'] = llm_key
                changed = True
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
    _walk(cfg)
    if not changed:
        return raw
    # Keep the blob's layout: the provider block is a single line, the
    # config files are indented.
    if '\n' in raw:
        return json.dumps(cfg, indent=2)
    return json.dumps(cfg, separators=(',', ':'))


# ── #3052: the coding default follows what the fleet serves ─────────────────
#: Agent types whose model resolves through another `agents.<key>` entry of
#: standard-models.yaml (catalog._coding_split_env uses `coding-tools`).
_TYPE_LLM_AGENT_KEY = {'opencode': 'coding-tools', 'gsd-pi': 'coding-tools',
                       'codex': 'coding-tools', 'user-defined': 'coding-tools'}

#: The env keys that carry a model choice or a model list. stale_wiring cannot
#: judge them while the served list is unreadable.
_FLEET_MODEL_ENV_KEYS = ('LLM_MODEL', 'AGENT_MODEL_ALIASES', 'OPENCODE_CONFIG_JSON',
                         'GSD_MODELS_JSON', 'PI_MODELS_JSON', 'CODEX_MODEL_CONTEXT_JSON',
                         'MOLTIS_PROVIDERS__OPENAI__MODELS', 'MOLTIS_CHAT__PRIORITY_MODELS')

#: The manager's /v1/models (same container and port as the key mint).
_LLM_MANAGER_MODELS_URL = 'http://llm-manager:8080/v1/models'

#: Sentinel: _resolve_env asks the manager itself.
_FETCH_SERVED = object()


def _coding_agent_key(agent_type: str | None) -> str | None:
    """The standard-models `agents.<key>` a type resolves through, when its
    role is `coding`; None for every other type (chat agents, admin-defined)."""
    from app.services import llm_config
    key = _TYPE_LLM_AGENT_KEY.get(agent_type or '', agent_type or '')
    if key not in (llm_config._spec.get('agents') or {}):
        return None
    return key if llm_config.role_of(key) == 'coding' else None


def _fetch_served_models(llm_key: str | None) -> list[str] | None:
    """The model ids the LLM Manager serves to this key, or None.

    None when there is no usable key (not an LLM-Manager box) or the manager
    cannot be read — the caller then keeps the configured defaults. The
    manager's /v1/models lists what its router can route right now (serveable
    instances only) and only what this key may call."""
    llm_key = _safe_llm_key(llm_key)
    if not llm_key:
        return None
    try:
        import httpx
        resp = httpx.get(_LLM_MANAGER_MODELS_URL,
                         headers={'authorization': f'Bearer {llm_key}'}, timeout=5)
        if resp.status_code != 200:
            logger.warning("served model list unavailable (HTTP %s) — agents keep "
                           "the configured model defaults", resp.status_code)
            return None
        data = resp.json().get('data')
        if not isinstance(data, list):
            raise ValueError('no data list')
        return [str(m['id']) for m in data if isinstance(m, dict) and m.get('id')]
    except Exception as e:  # noqa: BLE001
        logger.warning("served model list unavailable (%s) — agents keep the "
                       "configured model defaults", e)
        return None


def warn_flagged_model_with_open_egress(env: dict, agent_type: str | None,
                                        user_slug: str | None = None,
                                        policy_env=None) -> str | None:
    """#2962: the warning text (also logged) when the agent's model carries the
    catalogue flag `egress: denied-only` while AGENT_EGRESS_POLICY resolves to
    `open`; None otherwise. `policy_env` defaults to this process's environment,
    which is where the manager reads the policy (docker_client.proxy_env)."""
    from app.services import egress, llm_config
    model = env.get('LLM_MODEL')
    flag = llm_config.egress_flag(model)
    if flag != 'denied-only':
        return None
    if egress.resolve_policy(os.environ if policy_env is None else policy_env) != egress.POLICY_OPEN:
        return None
    msg = (f"agent {agent_type or '?'} for {user_slug or '?'} runs model {model}, which the "
           f"model catalogue flags egress: denied-only, while AGENT_EGRESS_POLICY=open: the "
           f"agent can reach the internet directly. Use another model for this agent, or "
           f"switch the box to AGENT_EGRESS_POLICY=allowlist with the egress overlay.")
    logger.warning("%s", msg)
    return msg


def _apply_fleet_served_models(env: dict, agent_type: str | None, served) -> None:
    """#3052 — point a coding agent at what the fleet serves.

    `served` None (unknown) or a non-coding type: no-op, the catalog values
    stand. Otherwise the agent's model follows llm_config.model_for(served=…)
    (a CPU-class default gives way to a served GPU-class coding model) and
    every model list it carries — opencode's provider block, gsd's / Pi's
    models.json, the session picker, moltis' list — names the served chat
    models, so the user can switch with `-m`. A value the template no longer
    carries in its catalog form (an admin edit) keeps its bytes."""
    if served is None:
        return
    key = _coding_agent_key(agent_type)
    if not key:
        return
    from app.services import llm_config
    try:
        configured = llm_config.model_for(key)
        chosen = llm_config.model_for(key, served=served)
        first = llm_config.alias_for(key, served=served)
        configured_alias = llm_config.alias_for(key)
        if env.get('LLM_MODEL') == configured:
            env['LLM_MODEL'] = chosen
        for k in ('AGENT_MODEL_ALIASES', 'MOLTIS_PROVIDERS__OPENAI__MODELS'):
            if k in env:
                env[k] = json.dumps(llm_config.alias_list(served, first))
        if env.get('MOLTIS_CHAT__PRIORITY_MODELS') == json.dumps([configured]):
            env['MOLTIS_CHAT__PRIORITY_MODELS'] = json.dumps([chosen])
        if 'OPENCODE_CONFIG_JSON' in env:
            cfg = json.loads(env['OPENCODE_CONFIG_JSON'])
            prov = cfg['provider']['gpustack']
            prov['models'] = llm_config.opencode_models(
                prov.get('options', {}).get('baseURL', _LLM_MANAGER_BASE_URL), served, first)
            if cfg.get('model') == f'gpustack/{configured_alias}':
                cfg['model'] = f'gpustack/{first}'
            env['OPENCODE_CONFIG_JSON'] = json.dumps(cfg)
        for k in ('GSD_MODELS_JSON', 'PI_MODELS_JSON'):
            if k in env:
                cfg = json.loads(env[k])
                prov = cfg['providers']['gpustack']
                prov['models'] = llm_config.gsd_models(
                    prov.get('baseUrl', _LLM_MANAGER_BASE_URL), served, first)
                env[k] = json.dumps(cfg, indent=2)
        # #3073: codex's catalog is built from LLM_MODEL + AGENT_MODEL_ALIASES
        # (above); without its context size a served model runs on codex's
        # 131072 default and compacts twice as early as the slot requires.
        if 'CODEX_MODEL_CONTEXT_JSON' in env:
            env['CODEX_MODEL_CONTEXT_JSON'] = json.dumps(llm_config.context_windows(served, first))
    except Exception:  # noqa: BLE001 — a malformed blob keeps the catalog values
        logger.warning("could not apply the served model list to %s", agent_type,
                       exc_info=True)


def _resolve_command_llm_placeholders(command, llm_key: str | None):
    """#959 D2 — resolve {{LLM_BASE_URL}}/{{LLM_API_KEY}} placeholders inside
    a catalog `command` (e.g. Hermes' `hermes config set model.base_url ...`
    bootstrap). An env-var rewrite can't reach a value baked into a shell
    command string, so catalog entries that need to follow the #612/#959
    LLM-Manager switch template the URL/key as placeholders here instead.

    No minted key: resolves to the GPUStack default endpoint and the literal
    `${GPUSTACK_API_KEY}` shell var, so the command is byte-identical to the
    pre-#959 literal and the container's own env
    supplies the key exactly as before. Minted key (llm-manager box):
    resolves to the manager's endpoint (same constant as the env-var switch)
    and the minted key itself.
    """
    if not command:
        return command
    # AGM-9 (#1039): the substitution lands inside a SHELL WORD for at least one
    # catalog entry, so a key carrying shell metacharacters must never reach it.
    # A rejected key takes the no-key branch below — the byte-identical pre-#959
    # command with `${GPUSTACK_API_KEY}` — rather than a half-quoted one.
    llm_key = _safe_llm_key(llm_key)
    if llm_key:
        base_url, api_key = _LLM_MANAGER_BASE_URL, llm_key
    else:
        # #1445 rev-C (review N1): the no-key branch KEEPS the GPUStack pair.
        # "No key" means the manager could not mint one — no manager container,
        # or (AGM-9) a key that failed the shell-safety check and was dropped
        # fail-closed. Pairing the canonical endpoint with ${GPUSTACK_API_KEY}
        # would be two halves that do not fit: the manager only accepts keys
        # carrying LLM_MANAGER_KEY_PREFIX and 401s a gpustack key, and on a box
        # without the manager profile the name `llm` does not resolve at all.
        # The fail-closed path must land on something that works, so it stays
        # byte-identical to the pre-#959 literal.
        base_url, api_key = _GPUSTACK_DEFAULT_BASE_URL, '${GPUSTACK_API_KEY}'
    return [
        part.replace('{{LLM_BASE_URL}}', base_url).replace('{{LLM_API_KEY}}', api_key)
        if isinstance(part, str) else part
        for part in command
    ]


def _type_uses_llm_endpoint(type_info: dict | None) -> bool:
    """AGM-3 (#1017) — does this catalog type actually talk to an LLM endpoint?

    The #612/#959 LLM-Manager switch is driven by a MINTED per-user key, and
    the mint used to be gated on `_is_assistant_net` — the NETWORK-fence set
    (`ASSISTANT_NET_TYPES = {'hermes'}`). That is a different question:
    opencode, codex, user-defined, coding-tools, moltis and
    paperclip all reference an LLM endpoint in their catalog entry but are not
    in that set, so no key was ever minted for them and
    `_apply_llm_manager_endpoint` was a permanent no-op — on an LLM-Manager box
    they kept the stack-wide GPUSTACK_API_KEY and a direct `gpustack:9090`,
    including the D3 substring swap written specifically for opencode's
    OPENCODE_CONFIG_JSON and user-defined's PI_MODELS_JSON.

    So the gate is the CATALOG, not the fence: a type needs a key iff its env
    template declares one of the LLM base-URL / key env vars, or bakes the
    gpustack endpoint (or `{{GPUSTACK_API_KEY}}`) into some other value, or its
    command carries the {{LLM_BASE_URL}}/{{LLM_API_KEY}} placeholders (#959
    D2). Types with no LLM wiring at all still mint nothing.

    NB this only decides whether to ASK llm-manager for a key.
    `_mint_llm_manager_key` returns None when there is no llm-manager
    container, so a GPUStack box remains byte-identical either way.
    """
    if not type_info:
        return False

    raw = type_info.get('env_template') or '{}'
    try:
        template = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        template = {}
    if isinstance(template, dict):
        for key, value in template.items():
            if key in _LLM_BASE_ENV_KEYS or key in _LLM_KEY_ENV_KEYS:
                return True
            if not isinstance(value, str):
                continue
            if ('gpustack:9090' in value
                    or _GPUSTACK_DEFAULT_BASE_URL in value
                    or _LLM_MANAGER_BASE_URL in value
                    or 'llm-manager:8080' in value
                    or '{{GPUSTACK_API_KEY}}' in value):
                return True

    command = type_info.get('command') or []
    if isinstance(command, str):
        try:
            command = json.loads(command)
        except (ValueError, TypeError):
            command = [command]
    for part in command or []:
        if not isinstance(part, str):
            continue
        if ('{{LLM_BASE_URL}}' in part or '{{LLM_API_KEY}}' in part
                or 'gpustack:9090' in part
                or _LLM_MANAGER_BASE_URL in part):
            return True
    return False


def _is_assistant_net(agent_type: str) -> bool:
    """W4a fence decision. Constant-only on purpose — see ASSISTANT_NET_TYPES."""
    return agent_type in ASSISTANT_NET_TYPES


def _is_coding_net(agent_type: str) -> bool:
    """#256 fence decision for the pre-split coding type — net only, no
    hardening. Constant-only, same authority pattern as the sets above."""
    return agent_type in CODING_NET_TYPES


def _is_sandboxed(agent_type: str, type_info: dict | None = None) -> bool:
    """Authoritative sandbox decision. True for the coding-agent split types.

    Also honours an explicit `sandbox: True` in the catalog SEED_TYPES dict if
    it's ever surfaced through the DB row, but the membership test is the source
    of truth so the sandbox can never be silently dropped."""
    if agent_type in SANDBOXED_TYPES:
        return True
    return bool(type_info and type_info.get('sandbox'))


class Provisioner:
    # #2476 (A21) defaults, so a Provisioner built without __init__ (several
    # test harnesses use __new__) behaves as "OpenBao not wired".
    _tokens = None
    _token_lock = threading.Lock()
    _owner_activity: dict = {}
    _owner_activity_at = float('-inf')

    def __init__(self, db, docker_client, caddy_client, catalog, config: dict,
                 authentik_client=None, readiness_gate: bool = False,
                 token_broker=None):
        # #2482 C6: hold `running` until a web pane's first request would
        # succeed (see _gate_running). create_app turns it on; the many
        # fake-docker unit fixtures leave it off, they have no UI to ask.
        self._readiness_gate = readiness_gate
        # #516: per-host consecutive-failure backoff for Authentik registration
        self._authentik_backoff = {}
        self._db = db
        self._docker = docker_client
        self._caddy = caddy_client
        self._catalog = catalog
        self._config = config
        self._authentik = authentik_client
        # #2476 (A21): hands each agent its owner's short-lived OpenBao token
        # (openbao_tokens.OpenBaoTokenBroker); None = OpenBao not wired.
        self._tokens = token_broker
        self._token_lock = threading.Lock()
        #: {owner uid: is_active} from the last COMPLETE Authentik listing.
        self._owner_activity = {}
        self._owner_activity_at = float('-inf')

    def _create_primary_container(self, agent_type: str, name: str, type_info: dict, *,
                                  env: dict, volumes: dict, mem_limit: str,
                                  instance_config: dict, labels: dict,
                                  version: str | None = None) -> str:
        """Create an instance's PRIMARY container (launch and upgrade alike).

        One place decides every per-type create flag, so launch and the
        upgrade recreate cannot drift apart. `version` defaults to the
        catalog's; the upgrade passes the target version. Companions are
        created separately and never get the #2476 secrets tmpfs.
        """
        from app.services.catalog import (CLAUDE_CODE_BLOCKED_HOSTS, CLAUDE_CODE_TYPES,
                                          SECRETS_TMPFS_UID)
        return self._docker.create_container(
            name=name,
            image=type_info['image'],
            version=version or type_info['version'],
            environment=env,
            volumes=volumes,
            mem_limit=mem_limit,
            cpu_limit=type_info['cpu_limit'],
            # #232 — the per-INSTANCE PID cap when the user set one,
            # else the per-type default (#221, coding family = 2048).
            # None falls back to docker_client's 512 fork-bomb floor.
            pids_limit=resolve_pids_limit(instance_config, type_info),
            docker_socket=type_info['requires_docker_socket'],
            labels=labels,
            # #959 D2: resolve {{LLM_BASE_URL}}/{{LLM_API_KEY}} placeholders
            # (e.g. Hermes' bootstrap) against the same minted-key switch
            # the env vars follow, on launch and on recreate.
            command=_resolve_command_llm_placeholders(
                type_info.get('command'), instance_config.get('_llm_manager_key')) or None,
            entrypoint=type_info.get('entrypoint') or None,
            # #36 security-review — sandbox the coding-agent split types.
            sandbox=_is_sandboxed(agent_type, type_info),
            assistant_net=_is_assistant_net(agent_type),
            coding_net=_is_coding_net(agent_type),
            # #2476: the owner-only /run/rzfz tmpfs for the owner's
            # short-lived OpenBao token (A21); None for upstream images.
            secrets_uid=SECRETS_TMPFS_UID.get(agent_type),
            # #2717: Claude Code's telemetry intake pinned to 0.0.0.0.
            blocked_hosts=(CLAUDE_CODE_BLOCKED_HOSTS
                           if agent_type in CLAUDE_CODE_TYPES else None),
        )

    # ── #2476 (A21): the owner's short-lived OpenBao token ───────────────────
    # Minted for the instance's OWNER (agent_instances.user_id, the stored
    # X-Authentik-Uid = the OIDC sub once A19's gate proved it), never for the
    # caller and never by username or slug. Every container start wipes the
    # /run/rzfz tmpfs, so each start delivers a fresh token and revokes the
    # previous one; stop and delete revoke; the reconcile replaces a token
    # before its 4 h period ends. Best-effort: OpenBao being down never fails
    # a lifecycle action, it only means no token. A revoke that fails is kept
    # in a pending list the reconcile drains.

    #: The token role's period is 4 h; a token older than this is replaced.
    TOKEN_ROTATE_AFTER_S = 3 * 3600
    #: An Authentik answer older than this (two reconcile intervals plus
    #: slack) no longer says anything about an owner.
    OWNER_ACTIVITY_MAX_AGE_S = 300
    _PENDING_REVOKES = 'openbao_pending_revokes'

    @staticmethod
    def _config_of(instance) -> dict:
        cfg = (instance or {}).get('config') or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    def _owner_known_active(self, instance, caller_uid=None) -> bool:
        """The owner acting (authenticated through Authentik just now), or
        a FRESH complete Authentik listing saying the owner is active. An
        admin's or a lazy start of someone else's agent with no such answer
        delivers nothing; the next token reconcile heals it."""
        owner = instance.get('user_id')
        if caller_uid and owner and caller_uid == owner:
            return True
        if time.monotonic() - self._owner_activity_at > self.OWNER_ACTIVITY_MAX_AGE_S:
            return False
        return self._owner_activity.get(owner) is True

    def _revoke_accessor(self, accessor) -> bool:
        """Revoke one accessor; a failure is queued for the reconcile."""
        try:
            self._tokens.revoke(accessor)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("openbao: revoking a token failed (%s); queued for the next pass",
                           type(e).__name__)
            self._queue_revoke(accessor)
            return False

    def _queue_revoke(self, accessor) -> None:
        try:
            pending = self._pending_revokes()
            if accessor not in pending:
                pending.append(accessor)
            self._db.set_agent_setting(self._PENDING_REVOKES, json.dumps(pending))
        except Exception as e:  # noqa: BLE001
            logger.error("openbao: could not queue a failed revoke (%s); the token "
                         "expires with its 4 h period", type(e).__name__)

    def _pending_revokes(self) -> list:
        try:
            v = json.loads(self._db.get_agent_setting(self._PENDING_REVOKES, '[]') or '[]')
        except (ValueError, TypeError):
            v = []
        return [a for a in v if isinstance(a, str) and a] if isinstance(v, list) else []

    def _drain_pending_revokes(self) -> int:
        # Under the token lock: every queueing caller (stop, delete, a
        # delivery replacing a token) holds it, so a revoke queued while the
        # queue is read and written back is never overwritten.
        with self._token_lock:
            pending = self._pending_revokes()
            if not pending:
                return 0
            left = []
            for acc in pending:
                try:
                    self._tokens.revoke(acc)
                except Exception:  # noqa: BLE001
                    left.append(acc)
            self._db.set_agent_setting(self._PENDING_REVOKES, json.dumps(left))
            return len(pending) - len(left)

    def _deliver_openbao_token(self, instance_id, caller_uid=None):
        """Deliver the owner's token into the running container; returns the
        accessor (kept as config['openbao_accessor']) or None. The previous
        token is revoked once the new one is in place (or none may be given)."""
        if not self._tokens:
            return None
        try:
            with self._token_lock:
                instance = self._db.get_instance(instance_id)
                if not instance:
                    return None
                cfg = self._config_of(instance)
                old = cfg.get('openbao_accessor')
                acc = self._tokens.deliver(
                    {**instance, 'instance_config': cfg}, instance.get('user_id'),
                    user_active=self._owner_known_active(instance, caller_uid))
                self._db.merge_instance_config(instance_id, {
                    'openbao_accessor': acc or None,
                    'openbao_issued_at': int(time.time()) if acc else None})
                if old and old != acc:
                    self._revoke_accessor(old)
                return acc
        except Exception as e:  # noqa: BLE001 — no token, never a failed start
            logger.warning("openbao: token delivery for instance %s failed: %s",
                           instance_id, type(e).__name__)
            return None

    def _revoke_openbao_token(self, instance) -> None:
        if not self._tokens or not instance:
            return
        try:
            with self._token_lock:
                fresh = self._db.get_instance(instance['id']) or instance
                acc = self._config_of(fresh).get('openbao_accessor')
                if not acc:
                    return
                # forgotten on the row first; a failed revoke stays queued
                self._db.merge_instance_config(instance['id'], {'openbao_accessor': None,
                                                                'openbao_issued_at': None})
                self._revoke_accessor(acc)
        except Exception as e:  # noqa: BLE001
            logger.warning("openbao: revoking the token of instance %s failed: %s",
                           instance.get('id'), type(e).__name__)

    def _reconcile_openbao_tokens(self, instances) -> dict:
        """Revoke the tokens of owners Authentik reports inactive or no longer
        lists; deliver to running agents of active owners that hold none (a
        lazy or admin start) and replace tokens near the end of their period;
        drain the queue of failed revokes. Nothing is decided on an
        incomplete listing."""
        if not self._tokens or not self._authentik:
            return {}
        out = {'tokens_revoked': 0, 'tokens_delivered': 0, 'tokens_rotated': 0}
        try:
            out['tokens_requeued_revoked'] = self._drain_pending_revokes()
        except Exception:  # noqa: BLE001
            logger.warning("openbao: draining the pending revokes failed", exc_info=False)
        can_deliver = getattr(self._tokens, 'enabled', lambda: True)()
        held = [r for r in instances if self._config_of(r).get('openbao_accessor')]
        if not can_deliver and not held:
            return out          # nothing to revoke, nothing to deliver: no listing
        activity = self._authentik.active_by_uid()
        if activity is None:
            return out
        self._owner_activity, self._owner_activity_at = activity, time.monotonic()
        from app.services.catalog import SECRETS_TMPFS_UID
        from app.services.openbao_tokens import is_technical
        now = time.time()
        for row in instances:
            active = activity.get(row.get('user_id')) is True
            cfg = self._config_of(row)
            acc = cfg.get('openbao_accessor')
            if acc and not active:
                self._revoke_openbao_token(row)
                out['tokens_revoked'] += 1
                continue
            if not (active and can_deliver and row.get('state') == 'running'
                    and row.get('agent_type') in SECRETS_TMPFS_UID
                    and not is_technical({'instance_config': cfg})):
                continue
            issued = cfg.get('openbao_issued_at')
            stale = acc and (not isinstance(issued, (int, float))
                             or now - issued > self.TOKEN_ROTATE_AFTER_S)
            if not acc or stale:
                if self._deliver_openbao_token(row['id']):
                    out['tokens_rotated' if acc else 'tokens_delivered'] += 1
        return out

    def _instance_host(self, agent_type: str, instance_id) -> str:
        """The per-instance forward-auth host (matches caddy_client's route)."""
        from app.services.caddy_client import instance_token
        agents_domain = self._config.get('AGENTS_DOMAIN', '')
        return f"{agent_type}-{instance_token(instance_id)}.{agents_domain}"

    def _register_authentik(self, agent_type: str, instance_id) -> bool:
        """Register the per-instance forward_single Authentik provider (PR #84
        C1). Best-effort: a failure logs but doesn't abort the launch — the C1
        anchors still fence the instance, and reconcile/retry re-registers.

        Returns True when the provider is in place (or Authentik is not
        configured, so there is nothing to register), False when the attempt
        failed. Callers that only want the side effect can keep ignoring the
        return; `repair_instance` (#244-H1) needs it, because a user clicking
        "Reconnect" must be told when only half the registration succeeded
        rather than seeing a green "repaired".
        """
        if not self._authentik:
            return True
        try:
            host = self._instance_host(agent_type, instance_id)
            # #516 backoff: a host that keeps failing (the prod loop: the same
            # nine 400s forever) must not be retried on every 120s tick — that
            # floods the log until real errors are invisible and hammers the
            # identity provider with work it will reject. Consecutive failures
            # back the host off exponentially (cap 30 min); any success resets.
            import time as _t
            # lazy init: test fixtures construct Provisioner without running
            # the full __init__, and a backoff cache must never be the thing
            # that turns registration into an AttributeError.
            backoff = getattr(self, "_authentik_backoff", None)
            if backoff is None:
                backoff = self._authentik_backoff = {}
            st = backoff.get(host)
            if st and _t.monotonic() < st["until"]:
                return False
            ok = self._authentik.register_instance(host)
            if ok:
                backoff.pop(host, None)
            else:
                fails = (st["fails"] if st else 0) + 1
                delay = min(120 * (2 ** (fails - 1)), 1800)
                backoff[host] = {
                    "fails": fails, "until": _t.monotonic() + delay}
                if fails in (1, 3) or fails % 10 == 0:
                    logger.warning(
                        "Authentik registration for %s failing (attempt %d) — "
                        "backing off %ds (#516)", host, fails, delay)
            # NB the old code returned True UNCONDITIONALLY here, ignoring
            # register_instance's False — 'Reconnect' reported repaired even
            # when the registration failed (#516 side-find). ok propagates now.
            return ok
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "Authentik register for %s/%s failed (non-fatal): %s",
                agent_type, instance_id, e)
            return False

    def _deregister_authentik(self, agent_type: str, instance_id):
        """Remove the per-instance Authentik provider on stop/delete."""
        if not self._authentik:
            return
        try:
            host = self._instance_host(agent_type, instance_id)
            self._authentik.deregister_instance(host)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "Authentik deregister for %s/%s failed (non-fatal): %s",
                agent_type, instance_id, e)

    # ── #2482 C6 ("warm before
    # running"): an agent with a web pane is `running` only once that pane's
    # first request would succeed. Two gates, both bounded, both probed by
    # app.services.outpost_probe:
    #   * the OUTPOST gate (types whose pane loads from the instance's own
    #     host, catalog.SUBDOMAIN_PANE_TYPES): the host passes the Authentik
    #     outpost. Not passing in time is an ERROR: the box's sign-in gateway
    #     does not serve the host.
    #   * the UI gate (every type with a web pane): the agent's UI answers
    #     below 500, as the proxy would ask it. Not answering in time is NOT an
    #     error (a slow UI is still booting): the row stays `provisioning`
    #     ("starting") and the state sync promotes it once the UI answers.
    #: The UI gate's outcome when the bound elapses. `provisioning` keeps the
    #: row "starting" — never `running` while the UI answers 5xx.
    _UI_PENDING_STATE = 'provisioning'

    def _subdomain_pane_host(self, agent_type: str, instance_id) -> str | None:
        """The instance host when the portal's pane loads FROM it (a type in
        catalog.SUBDOMAIN_PANE_TYPES) and a per-instance provider is registered
        for it; None when the host plays no part in the pane (`/i/<token>/`
        panes ride the portal host's static provider), when there is no
        Authentik client, or when the readiness gate is off."""
        from app.services.catalog import SUBDOMAIN_PANE_TYPES
        if (not getattr(self, '_readiness_gate', False) or not self._authentik
                or agent_type not in SUBDOMAIN_PANE_TYPES):
            return None
        return self._instance_host(agent_type, instance_id)

    def _ui_probe_target(self, agent_type: str, instance_id,
                         container_name: str | None) -> tuple[str, dict] | None:
        """(url, headers) of the request the manager's proxy sends for a
        pane's first load, or None for a type without a web pane (or with the
        readiness gate off). Same container (the companion for a companion-UI
        type), same port, and the `/i/<token>` prefix for a path pane."""
        if not getattr(self, '_readiness_gate', False) or not container_name:
            return None
        type_info = self._catalog.get_type(agent_type)
        if not type_info:
            return None
        from app.services.catalog import SUBDOMAIN_PANE_TYPES, ui_kind_for
        try:
            ports = (json.loads(type_info['ports']) if isinstance(type_info.get('ports'), str)
                     else type_info.get('ports')) or {}
        except (TypeError, ValueError):
            return None
        port = ports.get('internal')
        if not port or ui_kind_for(type_info) not in ('web', 'both'):
            return None
        target = container_name
        if type_info.get('companion_image'):
            target = f"{container_name}-{type_info.get('companion_suffix') or 'workspace'}"
        headers = {}
        if agent_type not in SUBDOMAIN_PANE_TYPES:
            from app.services.caddy_client import instance_token
            headers['X-Forwarded-Prefix'] = f"/i/{instance_token(instance_id)}"
        return f"http://{target}:{int(port)}/", headers

    def _access_restriction_error(self, agent_type: str, instance_id) -> str | None:
        """Why the box access-group restriction could not be put on this
        instance's sign-in entry at its last registration, or None. The client
        then removed the entry (fail closed), so the host is not served."""
        probe = getattr(getattr(self, '_authentik', None), 'restriction_error', None)
        if not callable(probe):
            return None
        try:
            why = probe(self._instance_host(agent_type, instance_id))
        except Exception:  # noqa: BLE001 — no verdict is not a failure
            return None
        return why if isinstance(why, str) and why else None

    def _gate_running(self, agent_type: str, instance_id, verb: str,
                      container_name: str | None) -> tuple[str, str | None]:
        """Wait (bounded) for both gates. Returns (state to write, message):
        ('running', None), ('error', why) or (_UI_PENDING_STATE, why).
        First, without waiting: an instance whose access-group restriction
        could not be applied is an error — its host is deliberately not served."""
        from app.services import outpost_probe
        why = self._access_restriction_error(agent_type, instance_id)
        if why is not None:
            logger.error("%s/%s: access-group restriction not applied (%s) — reporting error, "
                         "not running", agent_type, instance_id, why)
            return 'error', (
                f"{verb.capitalize()} failed: this box admits only the members of its access "
                f"group, and that restriction could not be put on the agent's sign-in entry, so "
                f"its address is not served. An administrator runs `rzfz post-install --refresh`; "
                f"then start the agent again.")
        host = self._subdomain_pane_host(agent_type, instance_id)
        if host is not None:
            ok, verdict, waited = outpost_probe.wait_until_passed(host)
            if not ok:
                logger.error("%s did not pass the Authentik outpost within %.0fs (last: %s) — "
                             "reporting error, not running (#2482)", host, waited, verdict)
                what = ("the outpost does not know this host yet (404)"
                        if verdict == outpost_probe.OUTPOST_404
                        else "the sign-in gateway did not answer")
                return 'error', (
                    f"{verb.capitalize()} failed: the agent runs, but its address {host} does not "
                    f"get past the sign-in gateway (Authentik outpost) after {waited:.0f} s: "
                    f"{what}. Its pane would only show the gateway's 'Not Found' page. Try Start "
                    f"again in a minute; if it persists, an administrator checks the Authentik "
                    f"outpost.")
            logger.info("%s passes the Authentik outpost after %.1fs (#2482)", host, waited)
        target = self._ui_probe_target(agent_type, instance_id, container_name)
        if target is not None:
            url, headers = target
            ok, verdict, waited = outpost_probe.wait_until_passed(
                probe=lambda: outpost_probe.probe_ui(url, headers))
            if not ok:
                logger.warning("%s: web UI not answering below 500 after %.0fs (last: %s) — "
                               "left starting, not running (#2482)", url, waited, verdict)
                return self._UI_PENDING_STATE, (
                    f"The agent is still starting: its web UI does not answer yet after "
                    f"{waited:.0f} s ({verdict}). It shows as running as soon as it does.")
        return 'running', None

    def _mark_running_when_reachable(self, agent_type: str, instance_id, verb: str,
                                     container_name: str | None = None,
                                     **state_kw) -> tuple[str, str | None]:
        """Write `running`, or what `_gate_running` says instead (#2482).
        Returns (state written, message or None)."""
        state, msg = self._gate_running(agent_type, instance_id, verb, container_name)
        if state == 'error':
            self._db.update_instance_state(instance_id, 'error',
                                           error_message=msg[:500], **state_kw)
        else:
            self._db.update_instance_state(instance_id, state, **state_kw)
        return state, msg

    def instance_host_ready(self, instance: dict) -> bool:
        """One probe per gate, no wait: may the state sync call this row
        `running`? True for every type without a web pane."""
        from app.services import outpost_probe
        if self._access_restriction_error(instance['agent_type'], instance['id']) is not None:
            return False
        host = self._subdomain_pane_host(instance['agent_type'], instance['id'])
        if host is not None and outpost_probe.probe_host(host) != outpost_probe.PASSED:
            return False
        target = self._ui_probe_target(instance['agent_type'], instance['id'],
                                       instance.get('container_name'))
        if target is not None and outpost_probe.probe_ui(*target) != outpost_probe.PASSED:
            return False
        return True

    @staticmethod
    def _owner_scope(instance: dict, username: str | None) -> tuple[tuple[str, ...], str | None]:
        """#1039: the owner match for a quota check on an EXISTING row — the
        same (slug candidates OR uid) match check_quota uses at launch.

        When the caller is the owner (the row's slug is one of the caller's
        slug candidates), every candidate is in scope, so the owner's
        instances under the other slug count. Otherwise (an admin acting on
        someone else's instance, or proxy.py's 'lazy-access' auto-start) only
        the row's own slug is known, never the caller's — the caller's own
        instances must not count against another user's cap. The row's uid
        is added in both cases (the Authentik uid; '' / None never matches).

        #2529: in the second case the row's recorded `_owner_slug` (the
        owner's canonical slug, written only for a row its owner opened) is in
        scope too, so a legacy-slug row's owner is counted under both slugs
        even when their rows carry different uids."""
        row_slug = instance['user_slug']
        candidates = tuple(slug_candidates(username)) if username else ()
        if row_slug in candidates:
            return candidates, instance.get('user_id')
        owner_slug = owner_slug_of(instance, _parse_config(instance.get('config')))
        slugs = (row_slug,) if owner_slug == row_slug else (row_slug, owner_slug)
        return slugs, instance.get('user_id')

    def check_quota(self, user_slug, user_groups: list[str],
                    agent_type: str, user_id: str | None = None) -> tuple[bool, str]:
        """Check if the user can launch an instance of this agent type.

        Returns (allowed, reason).

        AGM-8 (#1039): `user_slug` may be a single slug (str) OR an iterable of
        candidate slugs — `razzfazz_common.user_slug.slug_candidates(username)`
        = (current, legacy pre-hash). #192 taught the API/proxy layers to match
        on both; this counter did not, so an instance still stored under the
        legacy slug counted against neither `max_per_type` nor `max_running`
        and a user sitting at their cap could keep launching. The db counters
        take the same str-or-iterable shape (`= ANY(%s)`), so passing the
        candidate tuple through is all that is needed.

        #1039 follow-up: `user_id` (the Authentik uid, the same under both
        slugs) widens the max_per_type, max_heavy and max_running counts to
        the OWNER — a row counts when its slug is a candidate OR its uid
        matches. None/'' never matches.
        """
        tier = self._db.resolve_user_tier(user_groups)
        if not tier:
            return False, 'No agent access. Contact your admin to be added to an agent group.'

        type_info = self._catalog.get_type(agent_type)
        if not type_info:
            return False, f'Unknown agent type: {agent_type}'

        if not type_info['enabled']:
            return False, f'{type_info["display_name"]} is currently disabled.'

        # Check allowed_types
        allowed = tier.get('allowed_types')
        if allowed and agent_type not in allowed:
            return False, f'Your tier ({tier["display_name"]}) does not include {type_info["display_name"]}.'

        # Check per-type limit
        type_count = self._db.count_user_instances(user_slug, agent_type=agent_type, user_id=user_id)
        if type_count >= tier['max_per_type']:
            return False, f'You already have {type_count} {type_info["display_name"]} instance(s) (max {tier["max_per_type"]}).'

        # Check heavy tier limit
        if type_info['tier'] == 'heavy':
            heavy_count = self._db.count_user_instances(user_slug, tier='heavy', user_id=user_id)
            if heavy_count >= tier['max_heavy']:
                return False, f'You have reached your heavy agent limit ({tier["max_heavy"]}).'

        # #959/W3: per-user max-RUNNING-instances cap, enforced (not just
        # displayed). None/absent = unlimited, matching max_per_type/max_heavy.
        # This is a separate axis from max_per_type (total non-destroyed
        # instances of one type) — it caps how many instances of ANY type may
        # be simultaneously running.
        max_running = tier.get('max_running')
        if max_running is not None:
            running_count = self._db.count_user_running_instances(user_slug, user_id=user_id)
            if running_count >= max_running:
                return False, (f'You already have {running_count} agent(s) running '
                               f'(max {max_running}). Stop one first.')

        # Check global limit
        total = self._db.count_all_instances()
        if total >= self._config['AGENT_MAX_INSTANCES']:
            return False, 'System-wide agent instance limit reached. Try again later.'

        return True, 'OK'

    # ── Memory governance (#36 / PR #84) ──────────────────────────────────────

    def _per_instance_max_gb(self) -> int:
        return int(self._config.get('AGENT_MEM_PER_INSTANCE_MAX_GB', 16))

    def _default_mem_gb(self) -> int:
        return int(self._config.get('AGENT_MEM_DEFAULT_GB', 2))

    def resolve_mem_limit(self, tier: dict | None, type_info: dict,
                          requested_gb) -> str:
        """L1 — resolve the effective per-instance mem_limit ('Ng'), SERVER-SIDE.

        * A regular (non-power) tier ALWAYS gets the default — a requested value
          is ignored (the UI hides the field; the server enforces it).
        * A power/admin tier may raise it, CLAMPED to the per-instance max.
        * No/blank request → default. Below default → default (never smaller
          than the floor the agent needs).
        """
        default_gb = self._default_mem_gb()
        if not _tier_allows_custom_memory(tier):
            return f"{default_gb}g"
        try:
            gb = int(requested_gb)
        except (TypeError, ValueError):
            return f"{default_gb}g"
        gb = max(default_gb, min(gb, self._per_instance_max_gb()))
        return f"{gb}g"

    def effective_mem_budget_mb(self, configured_mb: int | None = None) -> int:
        """L2 — the effective global agents-memory budget in MB, BOUNDED by the
        host's REAL memory (host RAM − a core-stack safety reserve), computed
        LIVE. Never a hardcoded cap: on a big-RAM prod box the ceiling is high;
        on a Strix-Halo box (most RAM pinned as VRAM) it's tight.

        `configured_mb` is the operator-set budget (from agent_settings). The
        returned value is min(configured, host_ceiling) — an admin can't set a
        budget larger than the box can actually back.
        """
        reserve = int(self._config.get('AGENT_CORE_STACK_RESERVE_MB', 8192))
        host_total = 0
        try:
            host_total = int(self._docker.host_mem_total_mb())
        except Exception:  # noqa: BLE001
            host_total = 0
        host_ceiling = max(0, host_total - reserve) if host_total else 0
        if configured_mb is None:
            raw = self._db.get_agent_setting('global_mem_budget_mb', None)
            configured_mb = int(raw) if raw not in (None, '') else host_ceiling
        else:
            configured_mb = int(configured_mb)
        if host_ceiling:
            return min(configured_mb, host_ceiling)
        return configured_mb

    def _per_user_cap_mb(self) -> int | None:
        """L3 — the per-user memory cap in MB, or None if unset (no cap)."""
        raw = self._db.get_agent_setting('per_user_mem_mb', None)
        if raw in (None, ''):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def check_memory_budget(self, user_slug, mem_limit: str,
                            exclude_instance_mb: int = 0,
                            user_id: str | None = None) -> tuple[bool, str]:
        """L2 + L3 — refuse a launch/increase that would exceed the global
        budget or the per-user cap. `exclude_instance_mb` subtracts the current
        allocation of the instance being resized (so a raise counts only the
        DELTA against the sums). Returns (allowed, reason).

        #1039: `user_slug` is a slug or the owner's slug candidates, and
        `user_id` (Authentik uid) adds the owner's rows under any slug to the
        per-user sum."""
        from app.services.database import parse_mem_to_mb
        want_mb = parse_mem_to_mb(mem_limit)

        # Global budget (bounded by real host memory).
        budget = self.effective_mem_budget_mb()
        if budget and budget > 0:
            running = int(self._db.sum_running_mem_mb()) - int(exclude_instance_mb)
            if running + want_mb > budget:
                return False, (
                    'Agents memory budget exhausted — stop an instance or ask '
                    f'an admin to raise the budget (budget {budget} MB, in use '
                    f'{max(0, running)} MB, requested {want_mb} MB).'
                )

        # Per-user cap.
        cap = self._per_user_cap_mb()
        if cap and cap > 0:
            user_running = int(self._db.sum_user_running_mem_mb(user_slug, user_id=user_id)) - int(exclude_instance_mb)
            if user_running + want_mb > cap:
                return False, (
                    'Your personal agents memory cap would be exceeded — stop '
                    f'one of your agents first (cap {cap} MB, you are using '
                    f'{max(0, user_running)} MB, requested {want_mb} MB).'
                )
        return True, 'OK'

    def launch(self, agent_type: str, user_id: str, username: str,
               user_groups: list[str], user_config: dict = None,
               caller_uid: str | None = None) -> tuple[str | None, str]:
        """Launch a new agent instance. Returns (instance_id, message)."""
        user_slug = make_user_slug(username)
        # AGM-8 (#1039): dedup and quota must see the user's instances under
        # BOTH slugs (current + legacy pre-hash, #192) — an instance stored
        # under the legacy slug was invisible to the single-slug lookup, so
        # `launch` created a SECOND instance of the same type (the
        # `UNIQUE (agent_type, user_slug)` index does not stop it: the slugs
        # differ) and counted it against no quota. Everything the launch
        # CREATES (container name, volumes, per-instance DB) keeps using the
        # current slug — only the read-side lookups widen.
        lookup_slugs = slug_candidates(username)
        # #1988: instances are real. Without `new_instance` a launch of a type
        # the user already has keeps its old meaning - open (or resume) the
        # first instance, which is what every "Launch"/"Open" button meant.
        # With it, the quota decides (max_per_type is READ from here on) and a
        # further instance is created with its own number in every name.
        want_new = bool((user_config or {}).pop('new_instance', False)) if user_config else False

        # Check if already exists
        # #2596: ownership is uid-first. A new instance ALWAYS records the
        # owner's uid, and is refused when its slug (or the caller's legacy
        # slug, which two usernames can share: a.b / a_b) is already held by
        # a DIFFERENT uid.
        uid = str(user_id or '').strip()
        if not uid:
            return None, ('Your account has no stable user id, so an agent '
                          'cannot be created for it. Sign in again.')
        for held in lookup_slugs:
            others = {u for u in self._db.slug_holder_uids(held) if u != uid}
            if others:
                logger.warning("Launch refused for user=%r: slug %r is held by another user id",
                               username, held)
                return None, (f"The name '{held}' is already used by another "
                              "user's agents, so a new agent cannot be created "
                              "under it. Ask an administrator.")
        existing = self._db.get_instance_by_type_and_user(agent_type, lookup_slugs, user_id=uid)
        if existing and not want_new:
            # #1143: the stored state can be a lie — an externally removed
            # container leaves the row on 'running' and this branch then
            # answered "Instance already running." forever. Reconciled against
            # docker first, the same row lands on 'stopped' and takes the
            # resume path below, which recreates it (volumes survive by name).
            existing = self._reconcile_instance_state(existing)
            if existing['state'] == 'stopped':
                # #959/W3: resuming a stopped instance also raises the
                # RUNNING count, so it must respect the same max_running cap
                # a fresh launch does — pass user_groups through so start()
                # can enforce it.
                return self.start(existing['id'], username, user_groups,
                                  caller_uid=caller_uid)
            return str(existing['id']), f'Instance already {existing["state"]}.'

        # #2517 §3 (2026.09-ga.4): a launch that CREATES a row needs a name the
        # user does not already use (the resume path above stays name-free).
        # Checked here, before quota, keys and volumes, so a refusal costs
        # nothing; re-checked under the name lock by create_instance below,
        # which is the check that holds against a concurrent launch.
        type_info = self._catalog.get_type(agent_type)
        wanted = ((user_config or {}).get('custom_name') or '').strip()
        taken = self._db.user_instance_names(lookup_slugs)
        if not wanted:
            raise NameRequired(suggest_name((type_info or {}).get('display_name') or agent_type, taken))
        if name_key(wanted) in {name_key(t) for t in taken}:
            raise NameTaken(wanted, suggest_name(wanted, taken))

        # Quota check (AGM-8 — over both slug candidates, see above)
        allowed, reason = self.check_quota(lookup_slugs, user_groups, agent_type, user_id=user_id)
        if not allowed:
            return None, reason

        instance_no = self._db.next_instance_no(agent_type, lookup_slugs)
        sfx = instance_suffix(instance_no)
        container_name = f"agent-{agent_type}-{user_slug}{sfx}"

        # ── Memory governance (#36 / PR #84) ──────────────────────────────────
        # L1: resolve the effective per-instance mem_limit, SERVER-SIDE and
        # tier-gated (a regular user's requested value is ignored → default;
        # power/admin may raise it, clamped to the per-instance max).
        tier = self._db.resolve_user_tier(user_groups)
        mem_limit = self.resolve_mem_limit(
            tier, type_info, (user_config or {}).get('mem_gb'))
        # L2 + L3: refuse a launch that would exceed the global budget (bounded
        # by real host RAM) or the per-user cap.
        ok, mem_reason = self.check_memory_budget(lookup_slugs, mem_limit, user_id=user_id)
        if not ok:
            return None, mem_reason

        # Create DB record
        instance_config = user_config or {}
        instance_config['_generated_secret'] = secrets.token_urlsafe(24)
        # #165: mint a PER-USER Gitea token for the coding agents so they can
        # clone/push the box Gitea out of the box. Best-effort + persisted here
        # (in instance_config) so it survives an upgrade/recreate; empty → the
        # entrypoint skips the credential block and the user pastes their own PAT
        # (public clones still work via the entrypoint's insteadOf rewrite).
        # #2554: personal (the owner's own token, its name and owner recorded
        # for the revoke on delete, the Gitea user's name/email as the git
        # identity) or, for an admin, technical (a technical user's Gitea
        # service user). A refused technical launch creates nothing; it is
        # never downgraded to the owner's identity.
        try:
            if agent_type in SANDBOXED_TYPES:
                instance_config.update(self._gitea_credentials(
                    username, user_slug, user_groups, instance_config))
            elif _identity_mode(instance_config) != 'personal':
                return None, (f'{type_info.get("display_name") or agent_type} agents cannot run '
                              'as a technical user; only the coding agents can')
        except PermissionError as e:
            return None, str(e)

        # #612: on an LLM-Manager box, a fenced assistant gets a per-user
        # rzfz-sk key minted from llm-manager's peer-anchored internal
        # endpoint — metering/quota/audit then attribute every LLM call to
        # the agent's owner. Persisted on instance_config so the key (and
        # its id, for revoke-on-delete) survives an upgrade/recreate.
        # Best-effort: a mint failure leaves the catalog default env (the
        # agent runs LLM-less exactly as before #612 — never blocks launch).
        # NB deliberately OUTSIDE the SANDBOXED_TYPES block above — the two
        # sets are disjoint, and the first cut nested this unreachable
        # (#613 review blocker).
        # AGM-3 (#1017): gated on the CATALOG (does this type reference an LLM
        # endpoint at all?), not on the network-fence set — see
        # _type_uses_llm_endpoint. The old `_is_assistant_net` gate left every
        # coding agent on the stack-wide gpustack key.
        # #2554: a technical agent's LLM key is the technical user's (metered
        # to it, never to the admin who launched it).
        if _type_uses_llm_endpoint(type_info):
            minted = (self._mint_llm_manager_key(username, container_name)
                      if not _is_technical_config(instance_config) else
                      self._mint_llm_manager_key(_technical_user_of(instance_config), container_name))
            if minted:
                instance_config['_llm_manager_key'] = minted['key']
                instance_config['_llm_manager_key_id'] = minted['id']
        # Persist the chosen limit so it survives a recreate (upgrade() re-reads
        # config) and so the budget sums see the real per-instance allocation.
        instance_config['mem_limit'] = mem_limit
        # #1039: the creator IS the owner — recorded so a later recreate by
        # someone else (admin, lazy start, post-install re-key) mints any
        # missing llm-manager key for them, not for its caller.
        instance_config['_owner_username'] = username
        try:
            instance_id = self._db.create_instance(
                agent_type, user_id, user_slug, container_name, instance_config,
                instance_no=instance_no, name_scope=lookup_slugs
            )
        except NameTaken:
            # #2517: a concurrent launch took the name between the check above
            # and the locked re-check. Nothing was created; give back the key
            # minted for this attempt.
            self._revoke_llm_manager_key(instance_config, container_name)
            raise

        try:
            # Create volumes
            volumes = {}
            named_primary = {}   # E1 (#2518): recorded on the row below
            for vol_spec in (json.loads(type_info['volumes'])
                             if isinstance(type_info['volumes'], str)
                             else type_info['volumes']):
                # rc6.7 #91: support `host_path` bind mounts in addition to
                # named volumes (a file from STACK_HOST_PATH). Templated like
                # other catalog values.
                if 'host_path' in vol_spec:
                    host_path = _resolve_host_path(vol_spec['host_path'])
                    volumes[host_path] = vol_spec['mount']
                else:
                    vol_name = f"agent-{agent_type}-{user_slug}{sfx}-{vol_spec['name_suffix']}"
                    self._docker.create_volume(vol_name)
                    volumes[vol_name] = vol_spec['mount']
                    named_primary[vol_name] = vol_spec['mount']
            # E1 (#2518): record the names NOW, before anything else can fail,
            # so a recreate or delete of this row never has to recompute them.
            self._db.set_mounted_volumes(instance_id, {'primary': named_primary,
                                                       'companion': {}})

            # Create per-instance database if needed
            if type_info['requires_db']:
                self._create_instance_db(agent_type, user_slug, instance_config, instance_no)
                # AGM-12 (#1039): `_create_instance_db` sets `_db_password` on
                # the IN-MEMORY dict, but `create_instance` above already wrote
                # the JSONB config row — so the password was never persisted.
                # `upgrade()`/recreate re-resolves env from the stored row, and
                # `{{instance_db_password}}` then resolved to '' → paperclip's
                # DATABASE_URL lost its password on every recreate while the
                # postgres role still carried the real one. Persist it here.
                self._db.update_instance_config(instance_id, instance_config)

            # Resolve environment variables
            env = self._resolve_env(type_info, user_slug, username, instance_config,
                                    instance_id=instance_id, instance_no=instance_no)
            # #36 gap 2 — merge the user's OWN running MCP-proxy endpoints.
            # #2554: never into a technical agent: it gets none of the
            # launching person's personal tools.
            if not _is_technical_config(instance_config):
                env = self._inject_user_mcp(env, agent_type, user_slug,
                                            owner_id=user_id, username=username)

            # Create container
            ports = (json.loads(type_info['ports'])
                     if isinstance(type_info['ports'], str) else type_info['ports'])
            labels = {
                'razzfazz.managed': 'true',
                'razzfazz.agent.type': agent_type,
                'razzfazz.agent.user': user_slug,
                'razzfazz.agent.instance': str(instance_id),
            }

            container_id = self._create_primary_container(
                agent_type, container_name, type_info,
                env=env, volumes=volumes,
                # #36 / PR #84 — the tier-gated per-instance mem_limit (persisted
                # on instance_config above), not the catalog default.
                mem_limit=mem_limit,
                instance_config=instance_config, labels=labels,
            )

            # Start container
            self._docker.start_container(container_id)
            # #2476 (A21): the owner's OpenBao token, into /run/rzfz. The
            # caller counts as the owner only by the forward-auth uid header
            # (`caller_uid`), never by the username fallback in `user_id`.
            self._deliver_openbao_token(instance_id, caller_uid=caller_uid)

            # M020 S02 — companion container (e.g. hermes-workspace alongside
            # hermes-agent). Same instance, same network, separate container
            # lifecycle managed in lockstep with the primary in start/stop/delete.
            companion_image = type_info.get('companion_image')
            if companion_image:
                companion_suffix = type_info.get('companion_suffix') or 'workspace'
                companion_name = f"{container_name}-{companion_suffix}"
                companion_volumes = {}
                companion_vol_specs = (json.loads(type_info.get('companion_volumes') or '[]')
                                       if isinstance(type_info.get('companion_volumes'), str)
                                       else (type_info.get('companion_volumes') or []))
                for vol_spec in companion_vol_specs:
                    vol_name = f"agent-{agent_type}-{user_slug}{sfx}-{vol_spec['name_suffix']}"
                    self._docker.create_volume(vol_name)
                    companion_volumes[vol_name] = vol_spec['mount']
                self._db.set_mounted_volumes(instance_id, {'primary': named_primary,
                                                           'companion': companion_volumes})
                companion_env = self._resolve_env(
                    type_info, user_slug, username, instance_config,
                    template_field='companion_env_template',
                    instance_id=instance_id, instance_no=instance_no,
                )
                companion_labels = {**labels, 'razzfazz.agent.companion': 'true'}
                companion_id = self._docker.create_container(
                    name=companion_name,
                    image=companion_image,
                    version=type_info.get('companion_version') or 'latest',
                    environment=companion_env,
                    volumes=companion_volumes,
                    mem_limit=type_info['mem_limit'],   # shares the type's budget
                    cpu_limit=type_info['cpu_limit'],
                    docker_socket=False,                 # companions don't need it today
                    # #605 review: the pair must share the fence — a flat
                    # companion next to a fenced primary both breaks the pair
                    # (no common net) and halves the fence.
                    assistant_net=_is_assistant_net(agent_type),
                    coding_net=_is_coding_net(agent_type),
                    labels=companion_labels,
                    # #36: optional companion command override (e.g. hermes-
                    # workspace seeds its own config.yaml before exec'ing the
                    # workspace server). None → image's default entrypoint/CMD.
                    command=type_info.get('companion_command') or None,
                )
                self._docker.start_container(companion_id)
                logger.info(f"Launched companion {companion_name} (id={companion_id}) for {container_name}")

            # #606: NO per-instance Caddy route anymore. The static *.agents
            # wildcard (Caddyfile 27b) forward-auths and reverse-proxies every
            # instance host to this manager, whose proxy blueprint resolves
            # host -> instance (companion-aware) and bridges WebSockets —
            # live-verified 2026-08-23 (101 on the wildcard path, #603). A
            # route that cannot be lost needs no registration, no reconcile
            # and no watch.
            # PR #84 C1: register the per-instance Authentik forward-auth provider
            # so the embedded outpost authenticates this subdomain.
            self._register_authentik(agent_type, instance_id)

            # Update state. M030-S2: also stamp image_version so the
            # dashboard's update-available detector has accurate ground
            # truth from the first launch onward.
            # #2482: `running` only once the instance host passes the outpost
            # (the row stays `provisioning` meanwhile); `error` on the bound.
            gate_state, gate_msg = self._mark_running_when_reachable(
                agent_type, instance_id, 'launch', container_name,
                container_id=container_id,
                image_version=type_info['version'],
            )
            if gate_state == 'error':
                self._db.log_audit(user_id, 'launch_failed', agent_type, instance_id,
                                   {'error': gate_msg[:200]})
                return str(instance_id), gate_msg
            self._db.log_audit(user_id, 'launch', agent_type, instance_id,
                               {'container': container_name,
                                'image_version': type_info['version']})

            if gate_msg:      # #2482: the UI is still booting — "starting"
                return str(instance_id), f'{type_info["display_name"]} launched. {gate_msg}'
            return str(instance_id), f'{type_info["display_name"]} launched successfully.'

        except Exception as e:
            logger.exception(f"Failed to launch {container_name}")
            msg = self._friendly_launch_error(e, type_info)
            self._db.update_instance_state(
                instance_id, 'error', error_message=msg[:500])
            self._db.log_audit(user_id, 'launch_failed', agent_type, instance_id,
                               {'error': str(e)[:200]})
            return str(instance_id), msg

    @staticmethod
    def _friendly_launch_error(exc, type_info) -> str:
        """Translate raw docker errors into a clear, non-leaky user message
        (PR #84 review). A missing image (module not enabled / image never
        built or pulled on this box) is the common case for
        paperclip when its profile is off — surface an actionable message
        instead of a raw `404 Client Error ... No such image`.
        """
        text = str(exc)
        try:
            import docker.errors as _de
            is_missing = isinstance(exc, _de.ImageNotFound)
        except Exception:  # noqa: BLE001
            is_missing = False
        if is_missing or 'No such image' in text or 'not found' in text.lower() and 'image' in text.lower():
            image = f"{type_info.get('image', '?')}:{type_info.get('version', '?')}"
            display = type_info.get('display_name', type_info.get('id', 'This agent'))
            return (f"{display} isn't available on this box yet — its container "
                    f"image ({image}) hasn't been built or pulled. Enable the "
                    f"agent's module / run the post-install image pre-pull, then "
                    f"try again.")
        return f'Launch failed: {text}'

    def _companion_name(self, instance, type_info) -> str | None:
        """Return companion container name for this instance, or None.

        M020 S02 — companion containers are named `<primary>-<suffix>` where
        suffix defaults to 'workspace'. type_info may be None (e.g. when the
        catalog has been reseeded but the instance pre-dates the change);
        in that case we fall back to no-companion for graceful degradation.
        """
        if not type_info or not type_info.get('companion_image'):
            return None
        suffix = type_info.get('companion_suffix') or 'workspace'
        return f"{instance['container_name']}-{suffix}"

    # ── E1 (#2518): bind recreate + delete to the ACTUAL volumes ─────────────

    def _instance_containers(self, instance, type_info) -> list[dict]:
        """The instance's containers as docker has them (see
        `instance_containers`). Docker errors RAISE."""
        return instance_containers(self._docker, instance,
                                   self._companion_name(instance, type_info))

    def _pick_volumes(self, role: str, instance, live_mounts, persisted, specs):
        """One container's {name: mount} for a recreate, in the E1 order:
        live Mounts → persisted names → container_name pattern → REFUSE.
        Returns (volumes, source, added)."""
        cn = instance['container_name']
        expected = expected_volume_map(cn, specs)
        if live_mounts:
            base, source = dict(live_mounts), 'mounted'
        elif persisted:
            missing = [n for n in persisted
                       if not _ANON_VOLUME_RE.match(n) and not self._docker.volume_exists(n)]
            if missing:
                raise VolumeBindingRefused(
                    f"{role} volume(s) recorded for this instance are missing: "
                    f"{', '.join(sorted(missing))}. Refusing to recreate onto empty "
                    f"volumes — restore them or ask an admin.")
            base = {n: m for n, m in persisted.items()
                    if not (_ANON_VOLUME_RE.match(n) and not self._docker.volume_exists(n))}
            source = 'persisted'
        else:
            base = {n: m for n, m in expected.items() if self._docker.volume_exists(n)}
            source = 'name-pattern'
            if not base and specs:
                raise VolumeBindingRefused(
                    f"no {role} volumes found for {cn} (container gone, none recorded "
                    f"on the instance, none named {cn}-<suffix>). Refusing to create "
                    f"an empty set for an existing instance.")
            # Fix round 1 (#2518, controller ruling): with nothing recorded and
            # no container, a PARTIAL pattern match cannot tell a lost volume
            # from a catalog mount added later — refuse, like the persisted
            # path does for a missing recorded volume.
            missing = sorted(n for n in expected if n not in base)
            if base and missing:
                raise VolumeBindingRefused(
                    f"only part of the {role} volume set of {cn} exists; missing: "
                    f"{', '.join(missing)}. Refusing to recreate onto new empty "
                    f"volumes — restore them or ask an admin.")
        added = []
        for name, mount in expected.items():
            if not mount:
                continue
            if name in base:
                # Same volume, catalog moved its path: follow the catalog unless
                # another of the instance's volumes already sits there.
                if base[name] != mount and mount not in base.values():
                    base[name] = mount
            elif mount not in base.values():
                # A mount the catalog added since this instance was created.
                base[name] = mount
                added.append(name)
        return base, source, added

    def _resolve_instance_volumes(self, instance, type_info, live) -> tuple[dict, dict, dict]:
        """(primary {name: mount}, companion {name: mount}, info) for a recreate.
        Raises VolumeBindingRefused rather than inventing an empty set."""
        persisted = normalise_mounted_volumes(instance.get('mounted_volumes')) or {}
        live_primary = next((_mounts_of(b) for b in live if not b.get('companion')), None)
        # Fix round 1 (#2518): deterministic companion choice — the one under
        # the catalog's companion name first, then by name; logged when >1.
        expected_companion = self._companion_name(instance, type_info)
        companions = sorted((b for b in live if b.get('companion')),
                            key=lambda b: (b.get('name') != expected_companion, b.get('name') or ''))
        if len(companions) > 1:
            logger.warning("instance %s has %d companion containers (%s); taking the "
                           "volumes of %s", instance.get('id'), len(companions),
                           ', '.join(b.get('name') or '?' for b in companions),
                           companions[0].get('name'))
        live_companion = _mounts_of(companions[0]) if companions else None
        primary, p_src, p_added = self._pick_volumes(
            'primary', instance, live_primary, persisted.get('primary'),
            named_volume_specs(type_info, 'volumes'))
        companion, c_src, c_added = {}, 'none', []
        if type_info.get('companion_image'):
            companion, c_src, c_added = self._pick_volumes(
                'companion', instance, live_companion, persisted.get('companion'),
                named_volume_specs(type_info, 'companion_volumes'))
        info = {'primary': p_src, 'companion': c_src, 'added': p_added + c_added}
        if persisted and live_primary and set(live_primary) != set(persisted.get('primary') or {}):
            logger.warning(
                "instance %s: the live container mounts %s but the row recorded %s "
                "— recreating onto what is MOUNTED (#2518)", instance.get('id'),
                sorted(live_primary), sorted(persisted.get('primary') or {}))
        if info['added']:
            logger.warning("instance %s: adding catalog volume(s) %s (new mount "
                           "points; existing data volumes stay attached)",
                           instance.get('id'), info['added'])
        return primary, companion, info

    def _volumes_claimed_elsewhere(self, instance_id) -> set:
        """Every volume name another LIVE instance row could call its own: its
        recorded names plus both name stems (container_name and user_slug)
        applied to its type's catalog specs. Deliberately over-inclusive —
        a volume in here is never removed by someone else's delete."""
        claims = set()
        for row in (self._db.list_live_instance_volume_rows() or []):
            if str(row.get('id')) == str(instance_id):
                continue
            m = normalise_mounted_volumes(row.get('mounted_volumes'))
            if m:
                claims |= set(m['primary']) | set(m['companion'])
            ti = self._catalog.get_type(row.get('agent_type'))
            stems = {row.get('container_name'),
                     f"agent-{row.get('agent_type')}-{row.get('user_slug')}"
                     f"{instance_suffix(row.get('instance_no'))}"}
            for stem in filter(None, stems):
                for field in ('volumes', 'companion_volumes'):
                    claims |= set(expected_volume_map(stem, named_volume_specs(ti, field)))
        return claims

    #: #1143 — container states that POSITIVELY mean "this instance is not
    #: running". Absence (`get_container_state` -> None) is handled alongside
    #: them. Deliberately an allow-list of DEAD states rather than "anything
    #: that isn't 'running'": 'restarting' is on its way up, 'paused' is up and
    #: frozen, 'removing' is mid-teardown — demoting any of those would let a
    #: second container be launched onto the live one's volumes, which is far
    #: worse than telling the user to retry. An unknown/future docker state
    #: falls through the same way, so the guard fails CLOSED.
    DEAD_CONTAINER_STATES = frozenset({'exited', 'dead', 'created'})

    def stranded_networks(self, instance: dict, type_info: dict | None) -> list[str]:
        """#2589: '<container>: <network>' for every network the instance's
        containers (primary and companion) are pinned to that no longer
        exists. Empty when none is, or when docker cannot tell — "cannot
        tell" keeps the plain start."""
        out = []
        for name in filter(None, (instance.get('container_name'),
                                  self._companion_name(instance, type_info))):
            try:
                stale = self._docker.stale_networks(name)
            except Exception:  # noqa: BLE001
                logger.warning("stale-network check failed for %s", name,
                               exc_info=True)
                stale = None
            out += [f"{name}: {net}" for net in (stale or [])]
        return out

    def _reconcile_instance_state(self, instance: dict | None) -> dict | None:
        """#1143: re-read a 'running' DB row against docker reality.

        The store is not the truth about whether a container exists. An agent
        container removed OUTSIDE the manager — `docker rm` by an operator, an
        OOM kill with `restart=no`, a Docker upgrade — leaves the row saying
        'running' forever, and every guard that trusts the row then refuses to
        act: launch says "Instance already running.", start says "Already
        running.", repair 409s, and a manager restart changes nothing because
        the periodic sweep counted the ghost without ever clearing it. The only
        way out was delete + relaunch, which costs the user's workspace.

        So the row is demoted to 'stopped' when docker proves it wrong. Not
        'error': 'stopped' is the state whose existing handlers already do the
        right thing — `launch()` hands a stopped instance to `start()`, and
        `start()` either starts a container that is merely dead or, when it is
        GONE, falls into the A1 self-heal that recreates it from the DB row
        onto the surviving per-user named volumes (#625).

        Returns the instance dict, with `state` updated in place of the lie.
        Anything the check cannot positively disprove — a row that is not
        'running', a row with no container name, a docker that raises — is
        returned untouched, keeping the double-launch protection intact.
        """
        if not instance or instance.get('state') != 'running':
            return instance
        container_name = instance.get('container_name')
        if not container_name:
            return instance
        try:
            cstate = self._docker.get_container_state(container_name)
        except Exception:  # noqa: BLE001
            # Docker unreadable: we cannot prove the container is gone, so the
            # store's word stands. A second container spawned during a docker
            # hiccup is the worse failure.
            logger.warning(
                "state reconcile: could not read docker state for %s — "
                "keeping the stored 'running' (#1143)", container_name,
                exc_info=True)
            return instance
        if cstate is not None and cstate not in self.DEAD_CONTAINER_STATES:
            return instance

        logger.warning(
            "state reconcile: instance %s is stored as 'running' but its "
            "container %s is %s — demoting the row to 'stopped' so it can be "
            "started/recreated (#1143)", instance.get('id'), container_name,
            'GONE' if cstate is None else cstate)
        try:
            self._db.update_instance_state(instance['id'], 'stopped')
        except Exception:  # noqa: BLE001
            logger.exception(
                "state reconcile: could not persist the demotion for %s",
                instance.get('id'))
            return instance
        reconciled = dict(instance)
        reconciled['state'] = 'stopped'
        return reconciled

    def _tier_refusal(self, agent_type: str,
                      user_groups: list[str] | None) -> str | None:
        """#2661: may the CALLER's tier still run this agent type?

        check_quota() asks this at LAUNCH only. Start, recreate and upgrade of
        an EXISTING instance did not, so taking a type away from a tier (the
        admin API's `allowed_types`) left every instance already launched under
        it fully usable — including the ones that recreate with whatever the
        catalog grants the type. Returns the user-facing refusal, or None when
        allowed.

        Whose tier: the groups passed in are the CALLER's, not the instance
        owner's. That is intended — an administrator (unlimited-admin or
        agent-admin tier) may start or repair a user's instance for them; the
        owner, whose tier no longer includes the type, may not. Pinned by
        test_2661_no_raw_docker_socket.py::test_the_gate_reads_the_callers_tier.

        Deliberately NOT gated (they cannot bring a stopped instance back):
          * `user_groups=None` — internal callers with no identity
            (post-install re-key, volume migration);
          * `restart()` — acts only on a RUNNING instance (docker restart in
            place; a stopped or missing one is sent to Start);
          * `update_memory()`'s recreate fallback — reached only after a live
            `docker update` on a RUNNING container failed.
        The last two recreate, if at all, from the CURRENT catalog, so they
        cannot restore a socket the type no longer gets.
        """
        if user_groups is None:
            return None
        tier = self._db.resolve_user_tier(user_groups)
        if not tier:
            return ('No agent access. Contact your admin to be added to an '
                    'agent group.')
        allowed = tier.get('allowed_types')
        if allowed is not None and agent_type not in allowed:
            type_info = self._catalog.get_type(agent_type) or {}
            name = type_info.get('display_name') or agent_type
            return (f'Your tier ({tier.get("display_name") or tier.get("id")}) '
                    f'no longer includes {name}. Ask an administrator for '
                    f'access.')
        return None

    # ── #2589: one start / recreate per instance at a time ──────────────────

    def _instance_lock(self, instance_id) -> threading.RLock:
        """The per-instance lock start() and upgrade() hold. Reentrant: start()
        delegates its self-heals to upgrade() in the same thread. Created
        lazily (some callers build the provisioner with __new__)."""
        import uuid
        try:
            key = str(uuid.UUID(str(instance_id)))
        except ValueError:
            key = str(instance_id)
        guard = self.__dict__.setdefault('_instance_locks_guard', threading.Lock())
        with guard:
            return self.__dict__.setdefault('_instance_locks', {}).setdefault(
                key, threading.RLock())

    #: #3187 review round 3 — how long a mutation waits for the shared
    #: cross-process lock before refusing (another process mid-recreate).
    SHARED_LOCK_WAIT_SECONDS = 120.0

    @contextmanager
    def _shared_instance_lock(self, instance_id, wait_seconds=None):
        """Cross-process mutex for one instance: a PostgreSQL advisory lock
        held for the whole critical section, so a SECOND PROCESS (the
        `docker exec` CLI update, with its own provisioner and in-process
        locks) serializes against this one instead of recreating alongside
        it. Always acquired AFTER the in-process _instance_lock — the same
        order at every call site — so the lock order is process-local then
        shared and cannot deadlock. Reentrant in-process (start() delegating
        to upgrade() in one thread acquires once). When the DB offers no
        advisory lock (test doubles), it degrades to the in-process lock
        alone. Raises AdvisoryLockBusy when another process holds the lock
        beyond the wait window."""
        if wait_seconds is None:
            wait_seconds = self.SHARED_LOCK_WAIT_SECONDS
        key = sessions.activity_key(instance_id)
        guard = self.__dict__.setdefault('_shared_lock_guard', threading.Lock())
        with guard:
            depths = self.__dict__.setdefault('_shared_lock_depth', {})
            depth = depths.get(key, 0)
            depths[key] = depth + 1
        stack = ExitStack()
        try:
            if depth == 0:
                db_lock = getattr(self._db, 'instance_advisory_lock', None)
                if callable(db_lock):
                    stack.enter_context(
                        db_lock(instance_id, wait_seconds=wait_seconds))
            yield
        finally:
            with guard:
                depths = self.__dict__.setdefault('_shared_lock_depth', {})
                d = depths.get(key, 1) - 1
                if d > 0:
                    depths[key] = d
                else:
                    depths.pop(key, None)
            stack.close()

    def instance_busy(self, instance_id) -> bool:
        """True while another thread starts or recreates this instance; the
        state sync leaves such a row alone (its container is mid-recreate).
        #3187 review round 3: also True while ANOTHER PROCESS (the docker-exec
        CLI update) holds the shared advisory lock."""
        lock = self._instance_lock(instance_id)
        if lock.acquire(blocking=False):
            lock.release()
        else:
            return True
        held = getattr(self._db, 'instance_lock_held', None)
        if callable(held):
            try:
                if held(instance_id) is True:
                    return True
            except Exception:  # noqa: BLE001 — busy detection must not raise
                logger.debug("instance_busy: instance_lock_held failed",
                             exc_info=True)
        return False

    # ── #3187: live user attachments (portal PTY / proxied WS) ──────────────

    #: #3187 review round 3/4 — a session attached in ANOTHER process (the
    #: serving manager, while `rzfz agents update` runs via docker exec) is
    #: invisible to this process's registry, but while attached the serving
    #: process's SERVER-OWNED heartbeat (LifecycleManager `session_heartbeat`
    #: job, independent of inbound frames) keeps the shared row's
    #: last_accessed fresh, so a row fresher than this counts as attached.
    #: Bounded staleness: after detach or a crash the renewal stops and the
    #: row ages out past this window — a quiet attached PTY must never age
    #: out while still attached.
    SESSION_FRESHNESS_SECONDS = 90.0

    def session_active(self, instance_id, instance=None) -> bool:
        """True while a user terminal/session is attached to this instance —
        in THIS process (the registry; the manager runs as one worker,
        `-w 1`) or in ANOTHER process (the shared DB row's last_accessed is
        still fresh — the serving process renews it periodically for the
        full attachment lifetime, even with no inbound terminal frames).
        The idle auto-update pass and the _upgrade guard both read this — a
        busy user must only ever get the badge, never a recreate under
        them."""
        key = sessions.activity_key(instance_id)
        if sessions.is_active(key):
            return True
        row = instance
        if row is None:
            try:
                row = self._db.get_instance(instance_id)
            except Exception:  # noqa: BLE001 — fall back to the registry alone
                row = None
        last = row.get('last_accessed') if isinstance(row, dict) else None
        if not isinstance(last, datetime):
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age <= self.SESSION_FRESHNESS_SECONDS

    def activity_attach(self, instance_id) -> str:
        """Register a live attachment and bump last_accessed. MUST be called
        while holding the per-instance lock, so _upgrade's re-check under the
        same lock is race-free: an attach either lands before the recreate
        check or waits for the recreate and attaches to the NEW container."""
        key = sessions.attach(instance_id)
        try:
            self._db.update_last_accessed(instance_id)
        except Exception:  # noqa: BLE001 — idle bookkeeping must not break a session
            logger.debug("activity_attach: update_last_accessed failed", exc_info=True)
        return key

    def activity_detach(self, key: str, instance_id) -> None:
        """Drop the attachment; bump last_accessed once more so the idle
        window restarts at detach, not at the last keystroke."""
        sessions.detach(key)
        try:
            self._db.update_last_accessed(instance_id)
        except Exception:  # noqa: BLE001
            logger.debug("activity_detach: update_last_accessed failed", exc_info=True)

    #: #3187 — TTL for the per-instance update_available read cache. The card
    #: dashboard and /api/tree render per instance; without the cache each
    #: render would pay one docker inspect per instance (#1186 lesson).
    UPDATE_AVAIL_CACHE_TTL = 60.0

    def update_available(self, instance, type_info=None, use_cache=True) -> bool:
        """#3187 — reconciled per-instance update signal: version drift OR a
        proven image-ID drift of the running primary/companion container vs the
        catalog image (see app.services.agent_updates). One signal feeds the
        badge, the tree and the auto-update pass — never two divergent badges.
        """
        ti = type_info or self._catalog.get_type(instance['agent_type']) or {}
        key = str(instance.get('id') or instance.get('container_name'))
        cache = self.__dict__.setdefault('_update_avail_cache', {})
        now = time.monotonic()
        if use_cache:
            hit = cache.get(key)
            if hit and now - hit[0] < self.UPDATE_AVAIL_CACHE_TTL:
                return hit[1]
        val = agent_updates.update_available(self._docker, instance, ti)
        cache[key] = (now, val)
        return val

    def start(self, instance_id, username: str, *args, **kwargs) -> tuple[str, str]:
        """See _start(); serialised per instance (#2589)."""
        with self._instance_lock(instance_id):
            return self._start(instance_id, username, *args, **kwargs)

    def upgrade(self, instance_id, username: str, *args, **kwargs) -> tuple[str | None, str]:
        """See _upgrade(); serialised per instance in-process (#2589) AND
        across processes (#3187 review round 3): a docker-exec CLI update
        must never recreate alongside the serving manager, so the whole
        recreate also runs under the shared advisory lock."""
        try:
            with self._instance_lock(instance_id), \
                    self._shared_instance_lock(instance_id):
                return self._upgrade(instance_id, username, *args, **kwargs)
        except AdvisoryLockBusy:
            msg = ('Instance is being updated by another process — '
                   'try again in a moment.')
            logger.info("upgrade %s: %s", instance_id, msg)
            return None, msg

    def _start(self, instance_id, username: str,
               user_groups: list[str] | None = None,
               caller_uid: str | None = None) -> tuple[str, str]:
        """Start a stopped instance (and its companion if any).

        M031-FOLLOWUPS A1: if the container is missing entirely (e.g. a
        previous failed upgrade removed it before failing to recreate),
        delegate to upgrade() to recreate from scratch with the current
        catalog. Volumes are named by user_slug and survive the recreate.
        Without this self-heal the operator is stuck — start() 404s on the
        missing container, and the dashboard offers no other action.

        #959/W3: `user_groups`, when supplied, gates this start on the same
        per-user max_running cap check_quota() enforces at launch — a
        resumed instance raises the running count exactly like a fresh
        launch does. Optional (defaults to None = skip the check) so
        internal best-effort callers that don't carry an authenticated
        identity (proxy.py's lazy-access auto-start) are unaffected.
        """
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'
        refusal = removed_type_refusal(instance['agent_type'])
        if refusal:
            return None, refusal
        # #1143: ask docker before believing the row. Without this the guard
        # below shadowed the A1 self-heal a few lines down — a container that
        # had been removed externally could never be reached, because the row
        # still said 'running' and start() returned before it ever looked.
        instance = self._reconcile_instance_state(instance)
        if instance['state'] == 'running':
            return str(instance_id), 'Already running.'

        # #2661: the caller's tier first — a type the tier no longer includes
        # is refused before any cap/budget is counted or any self-heal runs.
        refusal = self._tier_refusal(instance['agent_type'], user_groups)
        if refusal:
            return None, refusal
        # #2554: a technical agent starts only while its technical user is
        # live and bound to Gitea (the binding recorded on the row).
        refusal = self._technical_refusal(self._config_of(instance))
        if refusal:
            return None, refusal

        # #1039: the OWNER's instances (every slug candidate when the caller
        # is the owner, plus the row's uid) — the same match check_quota uses.
        owner_slugs, owner_uid = self._owner_scope(instance, username)
        if user_groups is not None:
            tier = self._db.resolve_user_tier(user_groups)
            max_running = (tier or {}).get('max_running')
            if max_running is not None:
                running_count = self._db.count_user_running_instances(
                    owner_slugs, user_id=owner_uid)
                if running_count >= max_running:
                    return None, (f'You already have {running_count} agent(s) running '
                                   f'(max {max_running}). Stop one first.')

        type_info = self._catalog.get_type(instance['agent_type'])

        # #1039 review I2: starting a stopped instance adds its mem_limit to
        # the running sums exactly as a fresh launch does, so it passes the
        # same L2 (global budget) + L3 (per-user cap) check first — before the
        # self-heal below and before any container starts. launch()'s resume
        # path lands here too.
        ok, mem_reason = self.check_memory_budget(
            owner_slugs, _instance_mem_limit(instance, type_info), user_id=owner_uid)
        if not ok:
            return None, mem_reason

        # A1 self-heal: if the container is gone, run upgrade() which will
        # see the missing-container case (its remove step is already
        # tolerant of NotFound), then re-create with the current catalog.
        if self._docker.get_container_state(instance['container_name']) is None:
            logger.warning(
                f"start: container {instance['container_name']} missing — "
                f"falling back to upgrade()/recreate (A1 self-heal)"
            )
            # The caps above were checked for THIS start, so the recreate
            # may take the stopped instance to running (#1039 review I3).
            return self.upgrade(instance_id, username, start_stopped=True,
                                caller_uid=caller_uid)

        # #2589: a backup restore (and `rzfz down` + `start`) recreates the
        # stack's networks under new IDs. This container still names the old
        # ones, so `docker start` fails with "network … not found" and nothing
        # but a recreate brings it back. upgrade() rebuilds it from the row on
        # the same named volumes and attaches it to the live networks.
        stranded = self.stranded_networks(instance, type_info)
        if stranded:
            logger.warning(
                "start: %s is attached to networks that no longer exist (%s) — "
                "recreating it on the current ones (#2589)",
                instance['container_name'], ', '.join(stranded))
            # The tier gate and the caps above were checked for THIS start.
            # The SAME image version: a network repair is not an upgrade.
            return self.upgrade(instance_id, username,
                                target_version=instance.get('image_version'),
                                force=True, start_stopped=True,
                                caller_uid=caller_uid)

        # #2661: a container created while its type still held the raw host
        # docker socket keeps that bind for as long as it exists — starting it
        # again would hand the socket straight back. The upgrade re-key only
        # reaches RUNNING instances, so a stopped one is caught here and
        # recreated from the current catalog (named volumes survive). `is True`:
        # "cannot tell" (None) keeps the old start behaviour.
        if (not (type_info or {}).get('requires_docker_socket')
                and self._docker.holds_raw_docker_socket(
                    instance['container_name']) is True):
            logger.warning(
                "start: %s still holds the raw docker socket its type no "
                "longer gets (#2661) — recreating it", instance['container_name'])
            # The tier gate and the caps above were checked for THIS start, so
            # the recreate may take the stopped instance to running (#1039 I3);
            # without start_stopped it would be recreated and left stopped.
            return self.upgrade(instance_id, username, force=True,
                                start_stopped=True, caller_uid=caller_uid)

        # W4a (#256) self-healing migration — operator decision 3: an existing
        # assistant-class container is moved onto the fence net at its next
        # start. Best-effort by design: a migration failure logs and the start
        # proceeds on the old wiring (the next start retries).
        # #256: the same self-heal for the pre-split `coding-tools` type, whose
        # fence is `coding-agents`. An instance of it exists only on boxes that
        # predate the #36 split — precisely the ones that have been flat with
        # postgres/valkey/authentik the longest — and it is never recreated on
        # its own, so the start path is the only place the fix can reach it.
        _fence = ('agent-assistants' if _is_assistant_net(instance['agent_type'])
                  else 'coding-agents' if _is_coding_net(instance['agent_type'])
                  else None)
        if _fence:
            for _target in filter(None, (instance['container_name'],
                                         self._companion_name(instance, type_info))):
                try:
                    self._docker.migrate_to_fenced_net(_target, _fence)
                except Exception:
                    logger.exception(
                        "%s migration failed for %s — starting on "
                        "existing networks", _fence, _target)

        try:
            self._docker.start_container(instance['container_name'])
            # #2476 (A21): a start wipes /run/rzfz; a fresh owner token.
            self._deliver_openbao_token(instance_id, caller_uid=caller_uid)
            companion = self._companion_name(instance, type_info)
            if companion:
                try:
                    self._docker.start_container(companion)
                except Exception as ce:
                    logger.warning(f"Failed to start companion {companion}: {ce} (primary started ok)")

            # #606: no Caddy route to restore — the static wildcard carries
            # the instance host. Only the Authentik provider is re-registered
            # on start (idempotent — was deregistered on stop, PR #84 C1).
            self._register_authentik(instance['agent_type'], instance_id)

            # #2482: stop() deregistered the provider, so a resumed instance
            # meets the same outpost window as a fresh launch.
            gate_state, gate_msg = self._mark_running_when_reachable(
                instance['agent_type'], instance_id, 'start', instance['container_name'])
            if gate_state == 'error':
                return str(instance_id), gate_msg
            self._db.log_audit(instance['user_id'], 'start', instance['agent_type'], instance_id)
            if gate_msg:      # #2482: the UI is still booting — "starting"
                return str(instance_id), f'{instance["type_display_name"]} started. {gate_msg}'
            return str(instance_id), f'{instance["type_display_name"]} started.'
        except Exception as e:
            logger.exception(f"Failed to start {instance['container_name']}")
            return str(instance_id), f'Start failed: {e}'

    def update_memory(self, instance_id, username: str,
                      user_groups: list[str], mem_gb) -> tuple[str | None, str]:
        """#36 / PR #84 — change an instance's memory limit from the settings
        page. SERVER-SIDE tier-gated + budget-checked (the UI only reflects it).

        Sequence:
          1. Load the instance; resolve the caller's tier.
          2. L1 tier gate: only power/admin may change memory. A regular user
             is refused (returns None + a clear reason).
          3. Resolve + clamp the requested value (per-instance max).
          4. L2 + L3: refuse if the DELTA would exceed the global budget / the
             per-user cap (exclude this instance's current allocation).
          5. Apply: on a RUNNING container, `docker update --memory` LIVE (no
             restart). If the live update fails, fall back to a recreate via
             upgrade() (which reads the persisted mem_limit) and warn.
          6. Persist the chosen limit on instance.config so it survives a
             recreate and the budget sums see it.

        Returns (instance_id_str, message); instance_id_str is None on a hard
        refusal (not-found / tier-denied / budget-denied).
        """
        import uuid
        if isinstance(instance_id, str):
            # Best-effort UUID coercion (the API already validated the form).
            try:
                instance_id = uuid.UUID(instance_id)
            except (ValueError, TypeError):
                pass

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        tier = self._db.resolve_user_tier(user_groups)
        if not _tier_allows_custom_memory(tier):
            return None, ('Your tier does not permit changing agent memory. '
                          'Ask an admin to raise it.')

        type_info = self._catalog.get_type(instance['agent_type'])
        new_mem = self.resolve_mem_limit(tier, type_info or {}, mem_gb)

        # Current per-instance allocation (exclude it from the sums so only the
        # DELTA counts against budget/cap).
        from app.services.database import parse_mem_to_mb
        cfg = instance.get('config') or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except (ValueError, TypeError):
                cfg = {}
        current_mb = parse_mem_to_mb(cfg.get('mem_limit')
                                     or (type_info or {}).get('mem_limit') or '0')

        owner_slugs, owner_uid = self._owner_scope(instance, username)
        # #1039 review: only a RUNNING instance's allocation is in the running
        # sums, so only then is it excluded. Excluding a stopped one's too let
        # a resize of it pass with the cap already full.
        ok, reason = self.check_memory_budget(
            owner_slugs, new_mem,
            exclude_instance_mb=current_mb if instance['state'] == 'running' else 0,
            user_id=owner_uid)
        if not ok:
            return None, reason

        # Persist first so a recreate (fallback) reads the new value. #2536
        # item 20: only this key - a whole-row write undid a concurrent rename.
        cfg['mem_limit'] = new_mem
        self._db.merge_instance_config(instance_id, {'mem_limit': new_mem})

        applied_live = False
        if instance['state'] == 'running':
            try:
                self._docker.update_container_memory(
                    instance['container_name'], new_mem)
                applied_live = True
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"update_memory: live docker update failed for "
                    f"{instance['container_name']} ({e}); falling back to recreate")
                iid, msg = self.upgrade(instance_id, username,
                                        target_version=instance.get('image_version'))
                self._db.log_audit(
                    instance['user_id'], 'update_memory', instance['agent_type'],
                    instance_id, {'mem_limit': new_mem, 'method': 'recreate',
                                  'recreated': upgrade_succeeded(iid, msg)})
                if not upgrade_succeeded(iid, msg):
                    # Fix round 1 (#2518): never report "restarted" when the
                    # recreate was refused / aborted / failed.
                    return None, (f'Memory set to {new_mem}, but the agent could not '
                                  f'be restarted to apply it: {msg}')
                return iid, (f'Memory set to {new_mem} — the agent was restarted '
                             f'to apply it (live update was not possible).')

        self._db.log_audit(
            instance['user_id'], 'update_memory', instance['agent_type'],
            instance_id,
            {'mem_limit': new_mem, 'method': 'live' if applied_live else 'persisted'})
        if applied_live:
            return str(instance_id), f'Memory updated to {new_mem} (applied live).'
        return str(instance_id), (f'Memory set to {new_mem}; it takes effect the '
                                   f'next time this agent starts.')

    def _load_config(self, instance) -> dict:
        cfg = (instance or {}).get('config') or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except (ValueError, TypeError):
                cfg = {}
        return cfg

    def rename_instance(self, instance_id, username: str,
                        name: str, name_scope=None) -> tuple[str | None, str]:
        """#233 — set an instance's user-chosen name.

        `name` is expected ALREADY validated by `validate_custom_name` — the
        caller needs to distinguish "invalid input" (400) from "not yours"
        (404), and that distinction belongs at the API edge.

        2026.09-ga.4 (#2517 §3): a name is required and unique per user. An
        empty `name` raises NameRequired (it used to clear the override). With
        `name_scope` (the owner's slug candidates, canonical first — every API
        caller passes it) the write goes through `Database.set_instance_name`,
        which holds the name lock and raises NameTaken for a sibling's name.

        Persisted on `config` JSONB, so it survives an upgrade/recreate like
        every other per-instance setting. The manager UI, the portal tree and
        the start-portal tile pick it up immediately; the TERMINAL title does
        not, because `AGENT_LABEL` is an environment variable and a container's
        env cannot be changed in place. The caller tells the user that.
        """
        import uuid as _uuid
        if isinstance(instance_id, str):
            try:
                instance_id = _uuid.UUID(instance_id)
            except (ValueError, TypeError):
                pass

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        if not (name or '').strip():
            raise NameRequired(instance_display_name(instance))
        if name_scope is not None:
            self._db.set_instance_name(instance_id, name, name_scope)
        else:
            # #2536 item 20: only this key (a whole-row write undid a
            # concurrent memory or PID change).
            self._db.merge_instance_config(instance_id, {'custom_name': name})
        self._db.log_audit(instance.get('user_id'), 'rename',
                           instance['agent_type'], instance_id,
                           {'custom_name': name or None})

        return str(instance_id), (
            f'Renamed to “{name}”. The terminal’s own title bar picks this up '
            f'the next time the agent restarts.')

    def update_pids(self, instance_id, username: str, user_groups: list[str],
                    pids_limit: int) -> tuple[str | None, str]:
        """#232 — change an instance's PID cap.

        Tier-gated like memory, and for the same reason: PIDs are a shared
        kernel resource, so a raised cap is a whole-box risk rather than a
        personal preference. `pids_limit` is expected already validated.

        Unlike `update_memory`, a failed live update does NOT silently fall back
        to recreating the container. Memory can be argued either way; a PID cap
        is a background safety setting, and bouncing a running agent — killing
        whatever it is in the middle of — is a far bigger event than the change
        the user asked for. We persist and say plainly that it applies on the
        next start.
        """
        import uuid as _uuid
        if isinstance(instance_id, str):
            try:
                instance_id = _uuid.UUID(instance_id)
            except (ValueError, TypeError):
                pass

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        tier = self._db.resolve_user_tier(user_groups)
        if not _tier_allows_custom_memory(tier):
            return None, ('Your tier does not permit changing the PID limit. '
                          'Ask an admin to raise it.')

        # #2536 item 20: only this key (a whole-row write undid a concurrent
        # rename).
        cfg = self._load_config(instance)
        cfg['pids_limit'] = int(pids_limit)
        self._db.merge_instance_config(instance_id, {'pids_limit': int(pids_limit)})

        applied_live = False
        if instance['state'] == 'running':
            try:
                self._docker.update_container_pids(
                    instance['container_name'], int(pids_limit))
                applied_live = True
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "update_pids: live docker update failed for %s (%s); "
                    "persisted for next start", instance['container_name'], e)

        self._db.log_audit(
            instance.get('user_id'), 'update_pids', instance['agent_type'],
            instance_id,
            {'pids_limit': int(pids_limit),
             'method': 'live' if applied_live else 'persisted'})

        if applied_live:
            return str(instance_id), f'PID limit updated to {pids_limit} (applied live).'
        return str(instance_id), (
            f'PID limit set to {pids_limit}; it takes effect the next time this '
            f'agent starts (restart it to apply now).')

    def repair_instance(self, instance_id, username: str | None = None,
                        user_groups: list[str] | None = None) -> dict | None:
        """Force-re-register ONE instance's Authentik provider (#244-H1 —
        the "Reconnect / Repair" action).

        #606 removed the per-instance Caddy routes — the static *.agents
        wildcard carries every instance host, so there is no route half left
        to repair. What CAN still break per-instance is the Authentik
        forward-auth provider (#237 class); this re-registers it on demand
        instead of waiting for the periodic sweep tick.

        Forced, not skip-if-present: the user clicks *because* something is
        broken. `_register_authentik` is idempotent.

        #1143: when the container is GONE entirely, repair now RECREATES it
        instead of bailing out. That case — `docker rm` by an operator, an OOM
        kill with `restart=no`, a Docker upgrade — is precisely what the user
        clicks Reconnect for, and the old "Container is not running — use
        Start instead." 409 was a dead end: `start()` refused too, because the
        DB row still said 'running'. The recreate goes through `upgrade()`,
        the existing tested path for a missing container (#625): the per-user
        named volumes survive by name, so no workspace is lost.

        Returns ``{'authentik': bool}``, plus one of:
          * ``'recreated': bool`` — the container was missing and a recreate
            was attempted (the caller reports success/failure, never 409),
          * ``'skipped': True`` — the container EXISTS but is not running, so
            'Start' really is the right action and recreating it would throw
            away its writable layer for nothing.
        ``None`` when the instance is unknown.
        """
        import uuid as _uuid
        if isinstance(instance_id, str):
            instance_id = _uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None

        result = {'authentik': False}

        # Authentik independently of the container check: the provider is what
        # makes the host resolvable at all, and re-registering it is harmless
        # even if the container is down. `_register_authentik` is best-effort
        # and swallows its own errors, so take its verdict rather than relying
        # on an exception escaping.
        result['authentik'] = bool(
            self._register_authentik(instance['agent_type'], instance['id']))

        cstate = self._docker.get_container_state(instance['container_name'])
        if cstate is None:
            # #1143: the container does not exist. Recreate it from the DB row
            # — that IS the repair. `username` only feeds env templating on the
            # recreate ({{user_id}}); fall back to the slug when an internal
            # caller has no authenticated identity to pass.
            logger.warning(
                "repair: container %s is missing — recreating it from the DB "
                "row (#1143)", instance['container_name'])
            iid, msg = self.upgrade(instance['id'],
                                    username or instance['user_slug'],
                                    user_groups=user_groups)
            result['recreated'] = upgrade_succeeded(iid, msg)
            result['message'] = msg
            return result
        if cstate != 'running':
            result['skipped'] = True
        return result

    #: #716 — reaping is OPT-IN. A container with no DB row is unreachable
    #: garbage, but "no DB row" is also what a transient database error looks
    #: like from here, and deleting a user's live workspace on a hiccup is the
    #: worse failure. Surface always, delete only when an operator asked.
    REAP_ORPHANS_ENV = "RAZZFAZZ_REAP_ORPHAN_AGENT_CONTAINERS"

    def sweep_orphan_containers(self) -> dict:
        """Agent containers that no DB row owns. Returns {orphans, reaped}.

        Ownership is deliberately GENEROUS: a container counts as owned if its
        name is an instance's `container_name` **or** starts with one plus a
        dash. That second clause covers companions without asking the catalog
        for each row's `companion_suffix` — a lookup that returns None for an
        instance whose type was reseeded, which would turn a live companion
        into a false orphan. Erring towards "owned" costs a container that
        stays; erring the other way costs a user's workspace.
        """
        result = {"orphans": 0, "reaped": 0}
        owned = {row["container_name"] for row in self._db.get_all_instances()
                 if row.get("container_name")}
        live = self._docker.list_agent_containers()
        orphans = [c for c in live
                   if c["name"] not in owned
                   and not any(c["name"].startswith(o + "-") for o in owned)]
        if not orphans:
            return result

        result["orphans"] = len(orphans)
        logger.warning(
            "reconcile_routes: %d agent container(s) have no DB row (#716) — "
            "unreachable from My Agents, still holding memory/CPU: %s",
            len(orphans),
            ", ".join(f"{c['name']}({c['agent_type'] or '?'}/"
                      f"{c['user'] or '?'},{c['status']})" for c in orphans))

        if os.environ.get(self.REAP_ORPHANS_ENV) != "1":
            logger.warning(
                "reconcile_routes: not removing them — set %s=1 to reap "
                "(#716).", self.REAP_ORPHANS_ENV)
            return result

        for c in orphans:
            try:
                # No -v: the per-user named volumes outlive the container by
                # design (M030), and an orphan's volumes may still be the ones
                # its replacement is using.
                self._docker.remove_container(c["name"])
                result["reaped"] += 1
                logger.warning("reconcile_routes: reaped orphan container %s "
                               "(%s=1, #716)", c["name"], self.REAP_ORPHANS_ENV)
            except Exception:
                logger.exception(
                    "reconcile_routes: could not reap orphan %s", c["name"])
        return result

    def reconcile_routes(self) -> dict:
        """Periodic self-heal sweep: Authentik providers + ghost detection.

        HISTORY (#606): this used to re-register per-instance Caddy admin-API
        routes, which Caddy lost on every restart. Those routes are GONE —
        the static *.agents wildcard forward-auths and reverse-proxies every
        instance host to this manager (WS-capable, live-verified 2026-08-23),
        so there is nothing route-shaped left to heal. The name is kept
        because the lifecycle job id and operator muscle memory reference it;
        what remains under it is:

        * per-instance Authentik forward-auth provider reconcile (#237 class),
          container-checked FIRST (#516: ghost rows get NO registration),
        * the orphan-Authentik sweep (PR #84 C1 LOW-2).

        Returns a summary dict: {checked, reconciled, skipped, failed[, ghosts]}
        — ghosts = DB rows in state 'running' whose container is absent/stopped
        (#516). `reconciled` now counts Authentik re-registrations.
        """
        summary = {'checked': 0, 'reconciled': 0, 'skipped': 0, 'failed': 0}
        try:
            instances = self._db.get_all_instances()
        except Exception:
            logger.exception("reconcile_routes: could not enumerate instances")
            return summary

        for row in instances:
            if row.get('state') != 'running':
                continue
            summary['checked'] += 1
            try:
                # #516 ROOT CAUSE (order): Authentik was reconciled for every
                # DB row in state 'running' BEFORE anyone asked docker whether
                # the container still exists — prod carried nine ghost rows
                # whose containers were gone, and re-registered them against
                # Authentik every 120s tick, forever. The container check comes
                # FIRST now: a row whose container is absent/stopped gets NO
                # registration attempt; it is counted as a ghost and logged
                # once per sweep line, not per row per tick.
                instance = self._db.get_instance(row['id'])
                cstate = self._docker.get_container_state(
                    instance['container_name'])
                if cstate != 'running':
                    summary['ghosts'] = summary.get('ghosts', 0) + 1
                    # #1143: counting the ghost is not enough. Until the row
                    # itself is demoted, launch/start/repair all keep answering
                    # from the stale 'running' and the instance stays
                    # unreachable across manager restarts — the sweep is the
                    # only actor that runs without a user asking, so this is
                    # where the state has to heal. `_reconcile_instance_state`
                    # applies the same conservative rule as the interactive
                    # paths (a container in flux is left alone), so a 'paused'
                    # or 'restarting' ghost is reported but not demoted.
                    healed = self._reconcile_instance_state(instance)
                    if (healed or {}).get('state') != 'running':
                        summary['healed'] = summary.get('healed', 0) + 1
                    continue
                # PR #84 C1: reconcile the per-instance Authentik provider for
                # every ACTUALLY-RUNNING instance (idempotent).
                ok = self._register_authentik(row['agent_type'], row['id'])
                if ok:
                    summary['reconciled'] += 1
                else:
                    summary['skipped'] += 1
            except Exception:
                summary['failed'] += 1
                logger.exception(
                    "reconcile_routes: failed for instance %s", row.get('id'))

        if summary.get('ghosts'):
            logger.warning(
                "reconcile_routes: %d DB row(s) in state 'running' have no "
                "running container (ghosts, #516) — no registration attempted; "
                "delete or restart them via My Agents.", summary['ghosts'])

        # #2476 (A21): owner tokens follow the owner's Authentik state.
        try:
            summary.update(self._reconcile_openbao_tokens(instances))
        except Exception:
            logger.exception("reconcile_routes: OpenBao token reconcile failed")

        # #2554: a technical agent stops when its technical user is revoked.
        try:
            summary.update(self._reconcile_technical_instances(instances))
        except Exception:
            logger.exception("reconcile_routes: technical-user reconcile failed")

        # #716: the INVERSE of the ghost check above. That one finds DB rows
        # with no container; this one finds containers with no DB row — and
        # nothing looked for those, which is why one survived unnoticed for 98
        # minutes next to its replacement on 0.91 (2026-08-24).
        #
        # An orphan container is not merely untidy. Nothing in the UI can reach
        # it: start / stop / delete all go through a DB row, so it keeps its
        # memory and CPU reservation for as long as the box lives — on a 30 GB
        # box that is the #692 overcommit class, arriving silently. It also
        # still holds the per-user volumes and the tmux server its replacement
        # wants.
        try:
            summary.update(self.sweep_orphan_containers())
        except Exception:
            logger.exception("reconcile_routes: orphan-container sweep failed")

        # PR #84 C1 (LOW-2): sweep orphan Authentik forward-auth providers/apps
        # whose instance no longer exists (a failed-delete deregister or an
        # upgrade leftover). Build the set of live per-instance hosts from ALL
        # current DB instances (not just running — a stopped instance keeps its
        # provider only while running, but we key the sweep on existence so a
        # stopped-then-restarted instance isn't reaped mid-cycle). Best-effort.
        if self._authentik:
            try:
                live_hosts = set()
                for row in instances:
                    try:
                        live_hosts.add(self._instance_host(row['agent_type'], row['id']))
                    except Exception:  # noqa: BLE001
                        pass
                sweep = self._authentik.sweep_orphans(live_hosts)
                if sweep.get('orphans_removed') or sweep.get('failed'):
                    logger.info("reconcile_routes authentik sweep: %s", sweep)
            except Exception as e:  # noqa: BLE001
                logger.warning("reconcile_routes authentik sweep failed: %s", e)

        if summary['reconciled'] or summary['failed']:
            logger.info("reconcile_routes summary: %s", summary)
        return summary

    def stop(self, instance_id, username: str) -> tuple[str, str]:
        """Stop a running instance (preserve container + volumes). Includes companion if any."""
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        type_info = self._catalog.get_type(instance['agent_type'])
        companion = self._companion_name(instance, type_info)
        if companion:
            try:
                self._docker.stop_container(companion)
            except Exception as ce:
                logger.warning(f"Failed to stop companion {companion}: {ce}")
        self._docker.stop_container(instance['container_name'])
        # #2476 (A21): a stopped agent holds no live token.
        self._revoke_openbao_token(instance)
        # PR #84 C1: deregister the per-instance Authentik provider on stop
        # (re-registered on start). Keeps the outpost's provider list tidy.
        self._deregister_authentik(instance['agent_type'], instance_id)
        self._db.update_instance_state(instance_id, 'stopped')
        self._db.log_audit(instance['user_id'], 'stop', instance['agent_type'], instance_id)
        return str(instance_id), f'{instance["type_display_name"]} stopped.'

    def restart(self, instance_id, username: str,
                caller_uid: str | None = None) -> tuple[str | None, str]:
        """ga.2 (#219): restart a RUNNING instance in place (docker restart of
        the primary + companion), preserving all state.

        Non-destructive: the container, its named volumes, the Caddy route
        (its upstream is the unchanged container_name via docker DNS) and the
        per-instance Authentik provider all stay in place — only the process
        inside is bounced (SIGTERM → grace → SIGKILL → start). This is the
        recover-a-wedged-agent action (hung UI, stuck runtime) that keeps
        chats/skills/files/config intact.

        Only valid for a RUNNING instance: a stopped one must be *started*
        (Start re-registers the route + provider, which a bare docker restart
        would not), and a missing container is pointed at Start (which
        self-heals/recreates from the catalog). The DB state stays 'running'
        throughout — we never flip it, so the dashboard doesn't flicker to
        'stopped' mid-bounce.

        Returns (instance_id_str, message); instance_id_str is None only on
        not-found. A restart failure returns (id, 'Restart failed: …') so the
        API surfaces it as a 5xx and the dashboard renders the reason.
        """
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'
        if instance['state'] != 'running':
            return str(instance_id), 'Instance is not running — use Start instead.'
        if self._docker.get_container_state(instance['container_name']) is None:
            return str(instance_id), 'Container is missing — use Start to recreate it.'

        type_info = self._catalog.get_type(instance['agent_type'])
        companion = self._companion_name(instance, type_info)
        try:
            self._docker.restart_container(instance['container_name'])
            # #2476 (A21): the restart wiped /run/rzfz; a fresh owner token.
            self._deliver_openbao_token(instance_id, caller_uid=caller_uid)
            if companion:
                try:
                    self._docker.restart_container(companion)
                except Exception as ce:
                    logger.warning(
                        f"Failed to restart companion {companion}: {ce} "
                        f"(primary restarted ok)")
            # #244-H1 / #237: do NOT assume the route + provider survived the
            # bounce. They are dynamic (Caddy admin API + a per-instance
            # Authentik provider) and either can be missing — which is exactly
            # how a restarted agent used to come back 404ing until the periodic
            # reconcile caught up. Re-register both, best-effort: a repair
            # failure must not turn a successful restart into an error.
            try:
                repair = self.repair_instance(instance_id)
                if repair and not (repair.get('caddy') and repair.get('authentik')):
                    logger.warning(
                        "restart: re-registration incomplete for %s (%s)",
                        instance['container_name'], repair)
            except Exception:
                logger.exception("restart: re-registration failed for %s",
                                 instance['container_name'])
            self._db.log_audit(instance['user_id'], 'restart',
                               instance['agent_type'], instance_id)
            return str(instance_id), f'{instance["type_display_name"]} restarted.'
        except Exception as e:
            logger.exception(f"Failed to restart {instance['container_name']}")
            return str(instance_id), f'Restart failed: {e}'

    def _upgrade(self, instance_id, username: str,
                target_version: str | None = None,
                force: bool = False,
                start_stopped: bool = False,
                caller_uid: str | None = None,
                user_groups: list[str] | None = None) -> tuple[str | None, str]:
        """M030-S2: in-place upgrade of an instance to a new image version.

        #1039 review I3: an upgrade never takes a NOT-running instance to
        running on its own — that would bypass max_running and the memory
        caps, which only start()/launch() check. A row that is not 'running'
        is recreated and left STOPPED (the containers are created, not
        started). ``start_stopped=True`` is for start()'s A1 self-heal only,
        which has already passed those checks for this start.

        ``force=True`` (ga.1 iter3) skips the "same version + same image digest →
        nothing to do" short-circuit and ALWAYS stops + recreates the container
        with freshly-resolved env. Used by post-install's coding-agent re-key to
        re-inject the live GPUStack key into instances that were provisioned while
        the agent-manager still held the placeholder key (a pure env refresh, not
        an image change).

        Sequence:
          1. Look up the instance + current catalog type_info
          2. Determine target version (catalog default unless overridden)
          3. Run per-agent pre_stop_command (Q6 decision — moltis SQLite WAL
             flush, etc.) — best-effort, timeboxed
          4. Stop + remove containers (companion first); DO NOT remove volumes
          5. Re-create container(s) with the SAME named volumes (catalog
             vol_specs are unchanged; create_volume is idempotent for an
             existing name) but with the new image:version + freshly-resolved
             env (in case env_template gained new keys).
          6. Re-register Caddy route, mark instance running
          7. Stamp image_version + last_upgraded_at; audit-log "upgrade"
          8. On failure between steps 4-6, leave volumes intact and DB in
             "error" state so the operator can investigate or roll back via
             a fresh upgrade(target_version=<old>).

        Returns (instance_id_str, message). instance_id_str is None on
        not-found / invalid-input; the message is user-facing and surfaced
        in the dashboard toast.
        """
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'
        refusal = removed_type_refusal(instance['agent_type'])
        if refusal:
            return None, refusal
        # #1039 review I3: decided from the row BEFORE anything changes.
        run_after = start_stopped or instance.get('state') == 'running'

        type_info = self._catalog.get_type(instance['agent_type'])
        if not type_info:
            return str(instance_id), (f'Upgrade failed: catalog type {instance["agent_type"]!r} '
                                      f'not found — cannot upgrade.')

        refusal = self._tier_refusal(instance['agent_type'], user_groups)
        if refusal:
            return None, refusal

        # Resolve target image:version. Default = catalog's current version.
        new_version = target_version or type_info['version']
        old_version = instance.get('image_version') or '(unknown)'
        # #36 follow-up — the `:latest`→`:latest` reprovision no-op fix.
        # A naive tag-string compare (`new_version == image_version`) treated a
        # rebuilt mutable tag (`razzfazz-stack-paperclip:latest`, coding-agent
        # `:latest`, …) as "nothing to do" and short-circuited — so a rebuild
        # was never picked up without a sentinel `image_version` bump. When the
        # tag matches, we now also compare the container's ACTUAL running image
        # ID against what the tag resolves to locally: if a rebuild moved the
        # tag to a new digest, the container is stale → fall through to a
        # recreate. Only skip when tag matches AND the digest is unchanged
        # (a genuine no-op). See project_coding_tools_latest_tag_no_autoupdate.
        if not force and new_version == instance.get('image_version'):
            # #625: a MISSING container must never no-op — start()'s A1
            # self-heal delegates exactly this case here, and the cautious
            # "can't tell → don't force" rule below read the missing
            # container's None image-id as "no drift" and returned 'Already
            # on latest' — leaving the instance dead until a volume-destroying
            # delete+relaunch. Container gone (or its image-id unreadable
            # because it is gone) → fall through and recreate; the DB row
            # carries the full config and the per-user volumes survive by
            # name.
            if self._docker.get_container_state(instance['container_name']) is None:
                logger.info(
                    "upgrade: %s tag %s unchanged but the container is "
                    "MISSING — recreating from the DB row (#625).",
                    instance['container_name'], new_version)
            else:
                image_ref = f"{type_info['image']}:{new_version}"
                tagged_id = self._docker.get_image_id(image_ref)
                running_id = self._docker.get_container_image_id(
                    instance['container_name'])
                # Force a recreate only when we can positively confirm a drift
                # (both IDs known and different). Unknown/None on either side →
                # can't prove a rebuild, so keep the historical no-op behaviour
                # (for an EXISTING container — the missing case is handled
                # above and never lands here).
                stale = bool(tagged_id and running_id and tagged_id != running_id)
                # #3187 review: the companion image drifts independently of the
                # primary (its own tag). Companion-only drift must also fall
                # through to the recreate below — otherwise update_available
                # flags it but every upgrade() no-ops and the badge never clears.
                if not stale and type_info.get('companion_image'):
                    companion = self._companion_name(instance, type_info)
                    companion_ref = (f"{type_info['companion_image']}:"
                                     f"{type_info.get('companion_version') or 'latest'}")
                    stale = bool(agent_updates.image_drift(
                        self._docker, companion, companion_ref))
                    if stale:
                        logger.info(
                            "upgrade: %s tag %s unchanged but the COMPANION %s is "
                            "on an old image; recreating the pair onto the current "
                            "images.", instance['container_name'], new_version,
                            companion)
                if not stale:
                    return str(instance_id), f'Already on {new_version}; nothing to do.'
                logger.info(
                    "upgrade: %s tag %s unchanged but image rebuilt "
                    "(container %s → tag %s); recreating onto the new image.",
                    instance['container_name'], new_version,
                    (running_id or '')[:19], (tagged_id or '')[:19])

        # #3187 review: never tear down a container a user is attached to.
        # Checked HERE, under the per-instance lock AND the shared
        # cross-process lock upgrade() holds for the whole recreate —
        # attachments register under the SAME locks (in every process, so a
        # docker-exec CLI update sees them via the DB row), so an attach that
        # began after the caller's snapshot either lands before this check or
        # waits and attaches to the new container. force=True (post-install
        # re-key) intentionally bypasses.
        if not force and self.session_active(instance_id, instance=instance):
            msg = ('Instance has an active terminal session — not recreated '
                   'now; it updates automatically once idle.')
            logger.info("upgrade %s: %s", instance['container_name'], msg)
            self._db.log_audit(
                instance['user_id'], 'upgrade_skipped_active_session',
                instance['agent_type'], instance_id, {'reason': msg[:200]})
            return None, msg

        # 0. E1 (#2518): decide WHICH volumes the new container mounts BEFORE
        #    anything is stopped or removed — from the live containers' Mounts,
        #    else the names recorded on the row, else the container_name
        #    pattern. Never recomputed from row.user_slug (a migrated legacy
        #    row got a fresh EMPTY set that way on 2026-09-26). If nothing is
        #    found, refuse: the old container (if any) is left untouched.
        live = None
        try:
            live = self._instance_containers(instance, type_info)
            primary_vols, companion_vols, vol_info = self._resolve_instance_volumes(
                instance, type_info, live)
        except VolumeBindingRefused as e:
            msg = f'Recreate refused: {e}'
            logger.error("upgrade %s: %s", instance['container_name'], msg)
            if not any(not b.get('companion') for b in (live or [])):
                self._db.update_instance_state(instance_id, 'error', error_message=msg[:500])
            self._db.log_audit(instance['user_id'], 'upgrade_refused', instance['agent_type'],
                               instance_id, {'error': str(e)[:300]})
            # Fix round 1: a refusal is a FAILURE to every caller — None as the
            # id is what the API routes, repair, update_memory, volume_migrator
            # and post-install's re-key all read as "did not happen".
            return None, msg
        except Exception as e:  # noqa: BLE001 — docker unreadable: change nothing
            logger.exception("upgrade %s: could not read the instance's volumes",
                             instance['container_name'])
            return None, (f'Upgrade aborted before any change: could not '
                          f'read the instance\'s volumes from docker ({e}).')

        # #1039: a pre-AGM-12 row has no `_db_password`; recreated from it,
        # DATABASE_URL ships an empty one and the container restart-loops on
        # 28P01. Repair it while the old container still stands — keeping the
        # password it connects with — and refuse the recreate unless the row
        # then HAS a password. The start-up back-fill (services/instance_db.py)
        # normally got here first; this is the defence in depth for every
        # recreate path.
        if type_info.get('requires_db'):
            try:
                repair_config = _parse_config(instance.get('config'))
                if not repair_config.get('_db_password'):
                    _, repair_user = db_identity_of(repair_config, instance['agent_type'],
                                                    instance['user_slug'],
                                                    instance.get('instance_no') or 1)
                    self.ensure_db_password(instance['id'], repair_config, repair_user,
                                            instance['container_name'])
                instance['config'] = repair_config
            except Exception as e:  # noqa: BLE001
                logger.exception("upgrade %s: could not repair the missing DB password",
                                 instance['container_name'])
                return None, (f'Upgrade aborted before any change: could not repair '
                              f'the instance\'s database password ({type(e).__name__}).')
            if not repair_config.get('_db_password'):
                logger.error("upgrade %s (%s): no database password and it could not be "
                             "repaired (see the #1039 warning above) — not recreated",
                             instance['container_name'], instance['id'])
                return None, ('Upgrade aborted before any change: the instance has no '
                              'database password and it could not be repaired safely; '
                              'see the agent-manager log (#1039).')

        # 1. Pre-stop hook (Q6) — clean-shutdown the runtime before docker stop.
        self._run_pre_stop(instance, type_info)

        # 2. Stop + remove containers (companion first to avoid dangling reference).
        #    CRITICAL: remove_container without -v so named volumes survive.
        #    Fix round 1 (#2518): EVERY companion of this instance — the
        #    catalog-named one plus any found by the instance label under
        #    another name (as delete does) — so none survives the recreate.
        companion = self._companion_name(instance, type_info)
        extra_companions = sorted(b['name'] for b in (live or [])
                                  if b.get('companion') and b.get('name')
                                  and b.get('name') != companion)
        for cname in ([companion] if companion else []) + extra_companions:
            try:
                self._docker.remove_container(cname)
            except Exception as ce:
                logger.warning(f"upgrade: failed to remove companion {cname}: {ce}")
        try:
            self._docker.remove_container(instance['container_name'])
        except Exception as e:
            logger.exception(f"upgrade: failed to remove primary {instance['container_name']}")
            self._db.update_instance_state(instance_id, 'error', error_message=f'remove failed: {e}')
            return str(instance_id), f'Upgrade failed during stop: {e}'


        # 3. Re-create with new image onto the volumes resolved in step 0.
        #    Only catalog mounts the instance never had are created (additive);
        #    every volume it already used is re-attached by its real name.
        try:
            volumes = {}
            for vol_spec in (json.loads(type_info['volumes'])
                             if isinstance(type_info['volumes'], str)
                             else type_info['volumes']):
                if 'host_path' in vol_spec:
                    host_path = _resolve_host_path(vol_spec['host_path'])
                    volumes[host_path] = vol_spec['mount']
            for vol_name, mount in primary_vols.items():
                if vol_name in vol_info['added']:
                    self._docker.create_volume(vol_name)
                volumes[vol_name] = mount

            instance_config = (instance.get('config') or {})
            if isinstance(instance_config, str):
                instance_config = json.loads(instance_config)
            # AGM-3 (#1017): an instance provisioned before this fix (or before
            # llm-manager was enabled on the box) carries NO minted key, so its
            # env would be re-resolved onto the stack-wide gpustack credential
            # again. Mint lazily here — best-effort and a no-op on a GPUStack
            # box, where _mint_llm_manager_key returns None because there is no
            # llm-manager container. Persisted so the key (and its id, for
            # revoke-on-delete) survives the next recreate.
            #
            # #1039: the key is metered and budgeted to the username it is
            # minted for, so it is minted for the OWNER — never for whoever
            # triggered this recreate (an admin, proxy.py's 'lazy-access', the
            # post-install re-key, a user whose legacy slug collides).
            if (not instance_config.get('_llm_manager_key')
                    and _type_uses_llm_endpoint(type_info)):
                mint_for, record = key_owner_of(instance, instance_config,
                                                username, caller_uid)
                if _is_technical_config(instance_config):
                    # #2554: the technical user's key, whoever recreates it;
                    # none when the row names no valid technical user
                    mint_for, record = _technical_user_of(instance_config) or None, False
                minted = None
                if mint_for:
                    minted = self._mint_llm_manager_key(
                        mint_for, instance['container_name'])
                else:
                    logger.warning(
                        "upgrade: not minting an llm-manager key for instance %s "
                        "(%s): no owner is recorded on it and the caller %r is not "
                        "its authenticated owner. Deferred: the owner's next "
                        "visit records them, and the next recreate mints it.",
                        instance_id, instance['container_name'], username)
                if minted:
                    instance_config['_llm_manager_key'] = minted['key']
                    instance_config['_llm_manager_key_id'] = minted['id']
                    patch = {'_llm_manager_key': minted['key'],
                             '_llm_manager_key_id': minted['id']}
                    if record:
                        instance_config['_owner_username'] = mint_for
                        patch['_owner_username'] = mint_for
                    # Merged, not a whole-document write: a key another writer
                    # set since the read above (`_owner_slug`) must survive.
                    try:
                        self._db.merge_instance_config(instance_id, patch)
                    except Exception:  # noqa: BLE001 — env still gets the key
                        logger.warning("upgrade: could not persist the minted "
                                       "llm-manager key for %s",
                                       instance['container_name'], exc_info=True)
            # #36 / PR #84 — recreate with the SAME persisted per-instance
            # mem_limit (a power user's raised memory survives an upgrade);
            # fall back to the catalog default for pre-governance instances.
            recreate_mem = instance_config.get('mem_limit') or type_info['mem_limit']
            env = self._resolve_env(type_info, instance['user_slug'], username, instance_config,
                                    instance_id=instance['id'],
                                    instance_no=instance.get('instance_no') or 1)
            # #36 gap 2 — re-merge the user's MCP-proxy endpoints on relaunch
            # (picks up MCPs connected since the last launch).
            if not _is_technical_config(instance_config):   # #2554: no personal tools
                env = self._inject_user_mcp(env, instance['agent_type'],
                                            owner_slug_of(instance, instance_config),
                                            owner_id=instance.get('user_id'))
            ports = (json.loads(type_info['ports'])
                     if isinstance(type_info['ports'], str) else type_info['ports'])
            labels = {
                'razzfazz.managed': 'true',
                'razzfazz.agent.type': instance['agent_type'],
                'razzfazz.agent.user': instance['user_slug'],
                'razzfazz.agent.instance': str(instance_id),
            }

            # #36 security-review — the helper keeps the sandbox on
            # upgrade/recreate too (one place decides the hardening and the
            # #2476 secrets tmpfs for launch and upgrade alike).
            container_id = self._create_primary_container(
                instance['agent_type'], instance['container_name'], type_info,
                env=env, volumes=volumes, mem_limit=recreate_mem,
                instance_config=instance_config, labels=labels,
                version=new_version,
            )
            if run_after:
                self._docker.start_container(container_id)
                # #2476 (A21): the recreated container's /run/rzfz is empty.
                self._deliver_openbao_token(instance_id, caller_uid=caller_uid)
            else:
                self._revoke_openbao_token(instance)

            # Companion (e.g. hermes-workspace) — same dance with new version.
            if type_info.get('companion_image'):
                companion_suffix = type_info.get('companion_suffix') or 'workspace'
                companion_name = f"{instance['container_name']}-{companion_suffix}"
                companion_volumes = {}
                for vol_name, mount in companion_vols.items():
                    if vol_name in vol_info['added']:
                        self._docker.create_volume(vol_name)
                    companion_volumes[vol_name] = mount
                companion_env = self._resolve_env(
                    type_info, instance['user_slug'], username, instance_config,
                    template_field='companion_env_template',
                    instance_id=instance['id'],
                    instance_no=instance.get('instance_no') or 1,
                )
                companion_labels = {**labels, 'razzfazz.agent.companion': 'true'}
                companion_id = self._docker.create_container(
                    name=companion_name,
                    image=type_info['companion_image'],
                    version=type_info.get('companion_version') or 'latest',
                    environment=companion_env,
                    volumes=companion_volumes,
                    mem_limit=type_info['mem_limit'],
                    cpu_limit=type_info['cpu_limit'],
                    docker_socket=False,
                    # #605 review: fence the pair together (see provision path).
                    assistant_net=_is_assistant_net(instance['agent_type']),
                    coding_net=_is_coding_net(instance['agent_type']),
                    labels=companion_labels,
                    # #36: same companion command override as the provision
                    # path — the upgrade()'d workspace must re-seed its
                    # config.yaml too (fresh volume or new image).
                    command=type_info.get('companion_command') or None,
                )
                if run_after:
                    self._docker.start_container(companion_id)

            # #606: no Caddy route to re-register — the static wildcard
            # carries the instance host across the recreate unchanged.
            # PR #84 C1: (re)register the per-instance Authentik provider after
            # the upgrade recreate (idempotent — the host/token is unchanged).
            self._register_authentik(instance['agent_type'], instance_id)

            # E1 (#2518): record what the new containers mount — the next
            # recreate or delete reads these names back.
            self._db.set_mounted_volumes(instance_id, {
                'primary': primary_vols, 'companion': companion_vols})

            # 4. Mark upgraded — image_version + last_upgraded_at + the state
            #    the containers are actually in (#1039 review I3).
            gate_error = None
            if run_after:
                # #2482: `running` only once the pane's first load would work.
                gate_state, gate_msg = self._mark_running_when_reachable(
                    instance['agent_type'], instance_id, 'upgrade', instance['container_name'],
                    container_id=container_id, image_version=new_version)
                gate_error = gate_msg if gate_state == 'error' else None
            else:
                self._db.update_instance_state(
                    instance_id, 'stopped',
                    container_id=container_id, image_version=new_version,
                )
            self._db.mark_instance_upgraded(instance_id, new_version)
            # #3187: the recreate moved the container onto the current image —
            # drop the cached update_available so the badge clears at once.
            self.__dict__.get('_update_avail_cache', {}).pop(str(instance_id), None)
            if gate_error:
                self._db.log_audit(
                    instance['user_id'], 'upgrade_failed', instance['agent_type'], instance_id,
                    {'error': gate_error[:200], 'attempted_version': new_version})
                return str(instance_id), gate_error
            self._db.log_audit(
                instance['user_id'], 'upgrade', instance['agent_type'], instance_id,
                {'old_version': old_version, 'new_version': new_version},
            )
            # #36 follow-up: on a same-tag rebuild the human message
            # `latest → latest` is confusing — say it was recreated instead.
            if old_version == new_version:
                return str(instance_id), (
                    f'{instance["type_display_name"]} recreated onto the '
                    f'rebuilt {new_version} image.')
            return str(instance_id), (f'{instance["type_display_name"]} upgraded '
                                       f'{old_version} → {new_version}.')

        except Exception as e:
            logger.exception(f"upgrade: failed to recreate {instance['container_name']}")
            self._db.update_instance_state(
                instance_id, 'error', error_message=f'upgrade rollout failed: {e}')
            self._db.log_audit(
                instance['user_id'], 'upgrade_failed', instance['agent_type'], instance_id,
                {'error': str(e)[:200], 'attempted_version': new_version},
            )
            return str(instance_id), (f'Upgrade failed: {e}. Volumes preserved; '
                                       f'try again or contact admin.')

    def _run_pre_stop(self, instance, type_info) -> None:
        """M030-S2 Q6: run a per-agent clean-shutdown command via docker exec
        before stopping the container. Best-effort — failures and timeouts
        log a warning but don't block the stop.

        Catalog field shape:
          'pre_stop_command': ['sh', '-c', '...'] | None
          'pre_stop_timeout': 10 | None  (seconds)

        Examples:
          moltis: PRAGMA wal_checkpoint(FULL) on every .db file in
                  /home/moltis/.moltis to flush SQLite WAL → main DB.
        """
        cmd = type_info.get('pre_stop_command')
        if not cmd:
            return
        if isinstance(cmd, str):
            try:
                cmd = json.loads(cmd)
            except (ValueError, json.JSONDecodeError):
                logger.warning(f"pre_stop_command for {type_info['id']} is a string but not JSON; skipping")
                return
        timeout = type_info.get('pre_stop_timeout') or 10
        try:
            logger.info(f"pre-stop hook for {instance['container_name']}: {cmd}")
            self._docker.exec_in_container(instance['container_name'], cmd, timeout=timeout)
        except Exception as e:
            logger.warning(f"pre_stop hook failed for {instance['container_name']}: {e} "
                           f"(continuing with stop anyway)")

    def delete(self, instance_id, username: str) -> tuple[str, str]:
        """Delete an instance — remove container(s), volumes, and database."""
        import uuid
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)

        instance = self._db.get_instance(instance_id)
        if not instance:
            return None, 'Instance not found.'

        type_info = self._catalog.get_type(instance['agent_type'])

        # E1 (#2518): read what the containers ACTUALLY mount BEFORE they go —
        # afterwards the Mounts are gone with them. Companions are found by
        # the instance label (name fallback for pre-label containers).
        try:
            live = self._instance_containers(instance, type_info)
            live_ok = True
        except Exception:  # noqa: BLE001
            logger.warning("delete %s: could not read its containers from docker",
                           instance['container_name'], exc_info=True)
            live, live_ok = [], False

        # Stop and remove containers (companion first to avoid dangling reference)
        companion = self._companion_name(instance, type_info)
        companions = [b['name'] for b in live
                      if b.get('companion') and b.get('name') != companion]
        for cname in ([companion] if companion else []) + companions:
            try:
                self._docker.remove_container(cname)
            except Exception as ce:
                logger.warning(f"Failed to remove companion {cname}: {ce}")
        self._docker.remove_container(instance['container_name'])
        # #2476 (A21): the token goes with the agent.
        self._revoke_openbao_token(instance)
        # PR #84 C1: deregister the per-instance Authentik provider on delete.
        self._deregister_authentik(instance['agent_type'], instance_id)
        # #612: revoke the instance's rzfz-sk key (no-op on gpustack boxes /
        # instances launched before #612 — no key id on the config).
        _cfg = instance.get('config') or {}
        if isinstance(_cfg, str):
            _cfg = json.loads(_cfg)
        key_revoked = self._revoke_llm_manager_key(_cfg, instance['container_name'])
        # #2554: the instance's own Gitea token dies with it, revoked under
        # its OWNER's Gitea name (an admin may be the one deleting).
        self._revoke_gitea_token(_cfg.get('_gitea_user', ''), _cfg.get('_gitea_token_name', ''))

        # Remove volumes — E1 (#2518): exactly the ones this instance mounted
        # (live Mounts read above) or recorded on its row; only when neither
        # is known, the names its container_name implies. Never recomputed
        # from row.user_slug (after a slug migration that named volumes that
        # did not exist — the real ones survived as orphans — or, worse,
        # another instance's). bind mounts (rc6.7 #91 host_path) are never
        # volumes here.
        doomed = set()
        for b in live:
            doomed |= set(_mounts_of(b) or {})
        persisted = normalise_mounted_volumes(instance.get('mounted_volumes'))
        if persisted:
            doomed |= set(persisted['primary']) | set(persisted['companion'])
        if not doomed and type_info:
            for field in ('volumes', 'companion_volumes'):
                doomed |= set(expected_volume_map(instance['container_name'],
                                                  named_volume_specs(type_info, field)))
        if doomed and not live_ok and not persisted:
            logger.warning("delete %s: containers unreadable and no names recorded — "
                           "falling back to the container_name pattern",
                           instance['container_name'])
        try:
            claimed = self._volumes_claimed_elsewhere(instance_id)
        except Exception:  # noqa: BLE001 — cannot prove "not shared": keep all
            logger.warning("delete %s: could not read the other live instances — "
                           "keeping every volume (%s)", instance['container_name'],
                           ', '.join(sorted(doomed)), exc_info=True)
            doomed, claimed = set(), set()
        for vol_name in sorted(doomed):
            if vol_name in claimed:
                logger.warning("delete %s: NOT removing volume %s — another live "
                               "instance claims it (#2518)", instance['container_name'], vol_name)
                continue
            try:
                users = self._docker.containers_using_volume(vol_name)
            except Exception:  # noqa: BLE001
                users = None
                logger.warning("delete %s: could not check who mounts %s — keeping it",
                               instance['container_name'], vol_name, exc_info=True)
                continue
            if isinstance(users, list) and users:
                logger.warning("delete %s: NOT removing volume %s — still mounted by %s",
                               instance['container_name'], vol_name, ', '.join(users))
                continue
            try:
                self._docker.remove_volume(vol_name)
            except Exception as ve:
                logger.warning(f"Failed to remove volume {vol_name}: {ve}")

        # Drop per-instance database — #2529: the persisted names
        if type_info and type_info['requires_db']:
            db_name, db_user = db_identity_of(_cfg, instance['agent_type'],
                                              instance['user_slug'],
                                              instance.get('instance_no') or 1)
            self._drop_instance_db(db_name, db_user)

        self._db.delete_instance(instance_id)
        self._db.log_audit(instance['user_id'], 'delete', instance['agent_type'], instance_id)
        if key_revoked is False:
            self._db.log_audit(instance['user_id'], 'llm_key_revoke_failed',
                               instance['agent_type'], instance_id)
            return str(instance_id), (
                f'{instance["type_display_name"]} deleted, but revoking its LLM '
                f'key FAILED — the key may keep working for up to 5 minutes.')
        return str(instance_id), f'{instance["type_display_name"]} deleted.'

    # Keys whose VALUE is a secret; the report names them but never prints
    # what changed. The caller logs this into the post-install transcript.
    def stale_wiring(self, instance: dict, username: str | None = None) -> list | None:
        """Which baked values of a running instance no longer match a fresh
        provision — #1446 (cutover C6, point 4) and #785 LOW 1.

        An agent's wiring is resolved when its owner provisions it and then
        BAKED into the container: the LLM endpoint and key, the stack MCP
        registry, and the user's own MCP proxies (the per-user cognee proxy
        among them). Nothing re-reads any of it, so after `rzfz upgrade` an
        existing instance still points wherever the box pointed months ago —
        silently, because the container is healthy and running.

        Returns the sorted names of the values that differ, ``[]`` when the
        instance is in sync, and ``None`` when it cannot be told (container
        gone, catalog type missing, docker unreadable). ``None`` must never be
        read as "in sync": the caller decides what to do with "cannot tell",
        and saying so is the point.

        Names only, never values — the report lands in the post-install
        transcript, and half of these are keys.
        """
        try:
            type_info = self._catalog.get_type(instance.get('agent_type'))
        except Exception:  # noqa: BLE001
            return None
        if not type_info:
            return None
        container = instance.get('container_name')
        if not container:
            return None

        # #2661: the raw host docker socket on a type that no longer gets it is
        # stale wiring in its own right — not only when an env key happens to
        # change in the same release. Decided FIRST: an unreadable env, a
        # template that no longer resolves or a docker hiccup on the command
        # below must not turn a socket holder into "cannot tell" — it would
        # then be skipped by the re-key and keep the socket.
        socket_stale = (['(docker-socket)']
                        if (not type_info.get('requires_docker_socket')
                            and self._docker.holds_raw_docker_socket(container) is True)
                        else [])

        current_env = self._docker.get_container_env(container)
        if current_env is None:
            return socket_stale or None

        instance_config = (instance.get('config') or {})
        if isinstance(instance_config, str):
            try:
                instance_config = json.loads(instance_config)
            except Exception:  # noqa: BLE001
                instance_config = {}
        who = username or instance.get('user_id') or instance.get('user_slug') or ''
        # #3052: an unreadable served list says nothing about the model keys
        # — never recreate an agent onto the CPU fallback for a manager blip.
        asks_fleet = bool(_coding_agent_key(type_info.get('id'))
                          and _safe_llm_key(instance_config.get('_llm_manager_key')))
        served = (_fetch_served_models(instance_config.get('_llm_manager_key'))
                  if asks_fleet else None)
        try:
            wanted = self._resolve_env(type_info, instance.get('user_slug') or '',
                                       who, instance_config,
                                       instance_id=instance.get('id'),
                                       instance_no=instance.get('instance_no') or 1,
                                       served_models=served)
            if not _is_technical_config(instance_config):   # #2554: as at launch
                wanted = self._inject_user_mcp(wanted, instance.get('agent_type'),
                                               owner_slug_of(instance, instance_config),
                                               owner_id=instance.get('user_id'))
        except Exception:  # noqa: BLE001
            return socket_stale or None

        # #2717: CLAUDE_CODE_ENV is applied on the next launch, Reinstall or
        # Update, never by a forced recreate: a running agent that lacks only
        # it is in sync. Every other key stays strict.
        from app.services.catalog import CLAUDE_CODE_ENV
        stale = [key for key, value in wanted.items()
                 if key not in CLAUDE_CODE_ENV
                 and not (asks_fleet and served is None
                          and key in _FLEET_MODEL_ENV_KEYS)
                 and current_env.get(key) != str(value)]
        stale += socket_stale

        # An instance provisioned before this box had an LLM Manager carries no
        # minted key, so BOTH the env resolution above and the command
        # resolution below fall back to the pre-#612 GPUStack default — which
        # is exactly what its container already holds. It would compare as "in
        # sync" and never be re-wired, on the one kind of box where it is most
        # certainly stale. upgrade() mints the key lazily for precisely this
        # case (AGM-3 / #1017); the mint has side effects, so here we only
        # ASK whether the manager is there.
        if (not instance_config.get('_llm_manager_key')
                and _type_uses_llm_endpoint(type_info)):
            try:
                if self._docker.get_container_state('llm-manager') is not None:
                    stale.append('_llm_manager_key')
            except Exception:  # noqa: BLE001 — docker hiccup: cannot tell
                return socket_stale or None

        # Hermes carries its endpoint and key in the COMMAND, not the env; an
        # env-only comparison would call a stale hermes in sync.
        wanted_cmd = _resolve_command_llm_placeholders(
            type_info.get('command'), instance_config.get('_llm_manager_key')) or None
        if wanted_cmd:
            current_cmd = self._docker.get_container_command(container)
            if current_cmd is None:
                return socket_stale or None
            if list(current_cmd) != list(wanted_cmd):
                stale.append('(command)')
        return sorted(stale)

    def _resolve_env(self, type_info: dict, user_slug: str,
                     username: str, instance_config: dict,
                     template_field: str = 'env_template',
                     instance_id=None, instance_no=1,
                     served_models=_FETCH_SERVED) -> dict:
        """Resolve env_template placeholders to concrete values.

        `template_field` selects which JSONB column to resolve — defaults to
        'env_template' (primary container); pass 'companion_env_template' for
        the bundled companion (M020 S02).

        `instance_id` (M031-FOLLOWUPS B1/B2) — when provided, exposes
        {{instance_hash}} (the 8-hex-char DNS token from caddy_client) so
        env vars like SANDBOX_CONTAINER_URL_PATTERN and AGENT_INSTANCE_HOSTNAME
        can use the same hostname Caddy registers (`<type>-<hash>.agents.<domain>`).
        Fixes the long-standing "<type>-{slug} ≠ <type>-{hash}" mismatch
        and unblocks paperclip's auto-allowlist registration.

        `instance_no` (#2529): only for a row without persisted DB names.

        `served_models` (#3052): the LLM Manager's served model ids, None for
        "unknown"; by default a coding type asks the manager.
        """
        raw = type_info.get(template_field) or '{}'
        template = (json.loads(raw) if isinstance(raw, str) else raw)
        env = {}
        import os
        # #2717: Claude Code's own telemetry and error reporting off in every
        # primary container that can run it, an empty template included.
        # Applied after the template below, so no template value overrides
        # it. Launch and recreate both resolve through here; stale_wiring
        # deliberately ignores these keys, so an existing instance picks them
        # up on its next launch, Reinstall or Update and is never
        # force-recreated for them.
        from app.services.catalog import CLAUDE_CODE_ENV, CLAUDE_CODE_TYPES
        claude_code_env = (dict(CLAUDE_CODE_ENV)
                           if (template_field == 'env_template'
                               and type_info.get('id') in CLAUDE_CODE_TYPES)
                           else {})
        if not template:
            return claude_code_env
        # #2529: the names the per-instance database was created with.
        db_name, db_user = db_identity_of(instance_config, type_info.get('id', ''),
                                          user_slug, instance_no)
        db_pass = instance_config.get('_db_password', '')
        # Compute instance_hash if available
        instance_hash = ''
        if instance_id is not None:
            try:
                from app.services.caddy_client import instance_token
                instance_hash = instance_token(instance_id)
            except Exception:
                pass
        main_domain = os.environ.get('MAIN_DOMAIN', 'localhost')
        # #2554 review: ONE pass over each template value. A substituted value
        # (a Gitea display name, the user's custom name, a token) is data and
        # is never scanned again, so a placeholder inside it cannot expand to
        # a box secret named later in the table. An unknown placeholder stays.
        values = {
            'user_slug': user_slug,
            'user_id': username,
            'instance_hash': instance_hash,
            'generated_secret': instance_config.get('_generated_secret', ''),
            'MAIN_DOMAIN': main_domain,
            'AGENTS_DOMAIN': os.environ.get('AGENTS_DOMAIN', f"agents.{main_domain}"),
            'GPUSTACK_API_KEY': os.environ.get('GPUSTACK_API_KEY', ''),
            'VALKEY_PASSWORD': os.environ.get('VALKEY_PASSWORD', ''),
            # ── Gitea checkout wiring (#165) ─────────────────────────────────
            # {{gitea_token}} → the PER-USER token minted at launch (stored on
            # instance_config so it survives an upgrade/recreate); empty when
            # Gitea is off / the user isn't in Gitea yet. {{GITEA_EXTERNAL_URL}}
            # / {{GITEA_INTERNAL_URL}} give the container the external form to
            # rewrite (insteadOf) and the reachable internal endpoint.
            'gitea_token': instance_config.get('_gitea_token', ''),
            # #2554: the owner's Gitea identity (Gitea attributes a commit by
            # its author email); the slug identity when none was recorded.
            'git_name': instance_config.get('_git_name') or user_slug,
            'git_email': instance_config.get('_git_email') or f"{user_slug}@{main_domain}",
            'GITEA_EXTERNAL_URL': _gitea_external_url(),
            'GITEA_INTERNAL_URL': _gitea_internal_url(),
            # #2598: no {{AUTHENTIK_BOOTSTRAP_PASSWORD}} placeholder. It put the
            # Authentik admin's password within reach of any agent type an admin
            # defines; no shipped template used it.
            # Per-instance DB placeholders (#2529: resolved once, above)
            'instance_db_name': db_name,
            'instance_db_user': db_user,
            'instance_db_password': db_pass,
            'valkey_db_index': '10',
            # #233 — the user's own name for this agent, surfaced INSIDE the
            # container so the terminal titles itself with it.
            'custom_name': instance_config.get('custom_name', ''),
        }

        def _sub(m):
            v = values.get(m.group(1))
            return m.group(0) if v is None else str(v)
        for key, value in template.items():
            env[key] = _PLACEHOLDER.sub(_sub, str(value))

        # An EMPTY AGENT_LABEL is not the same as an absent one: the container
        # reads `os.environ.get("AGENT_LABEL", AGENT_KIND)`, so passing '' wins
        # over the fallback and titles the window with nothing at all. Drop the
        # key instead, and the image's own default stays in charge.
        if env.get('AGENT_LABEL') == '':
            del env['AGENT_LABEL']

        env.update(claude_code_env)    # #2717, see above

        # #612/#959: LLM-Manager boxes — repoint the LLM backend at the
        # manager's OpenAI ingress with the per-user rzfz-sk key. Only when a
        # key was actually minted (i.e. this IS an LLM-Manager box and the
        # mint succeeded); gpustack boxes keep the catalog defaults untouched.
        _apply_llm_manager_endpoint(env, instance_config.get('_llm_manager_key'))

        # #3052: a coding agent's model and model lists follow what the fleet
        # serves (a CPU master with a joined GPU worker hands out the GPU
        # model). Unreadable list -> the configured defaults, logged.
        if template_field == 'env_template' and _coding_agent_key(type_info.get('id')):
            if served_models is _FETCH_SERVED:
                served_models = _fetch_served_models(instance_config.get('_llm_manager_key'))
            _apply_fleet_served_models(env, type_info.get('id'), served_models)

        # #2962: a model the catalogue flags `egress: denied-only` must not run
        # an agent with open egress. Until the egress settings exist the
        # manager does not refuse; it warns, every time such an agent starts.
        if template_field == 'env_template':
            warn_flagged_model_with_open_egress(env, type_info.get('id'), user_slug)

        # #245: LLM-trace observability for personal agents. The endpoint
        # arrives via OTEL_AGENTS_ENDPOINT (compose seam, set by
        # post-install only while the observability profile is active).
        # Standard OTEL SDK env — agents without an SDK ignore it; the
        # service name attributes the traces per agent type + user.
        #
        # The PROTOCOL is stated, not left to the SDK's default. The endpoint
        # above is the collector's OTLP/HTTP receiver (:4318); its gRPC
        # receiver is :4317. Which transport an unset
        # OTEL_EXPORTER_OTLP_PROTOCOL means is NOT the same answer in every
        # SDK — the spec says http/protobuf, several language distros have
        # historically defaulted to grpc — and we inject into whatever agent
        # image the operator runs. An SDK that picks grpc would speak it at
        # the HTTP port and drop every span in a background batch processor,
        # with no error visible anywhere. Same failure Dify was one tidied-up
        # example line away from (#245).
        otel_ep = (os.environ.get('OTEL_AGENTS_ENDPOINT') or '').strip()
        if otel_ep:
            env.setdefault('OTEL_EXPORTER_OTLP_ENDPOINT', otel_ep)
            env.setdefault('OTEL_EXPORTER_OTLP_PROTOCOL', 'http/protobuf')
            _t = type_info.get('id') or 'agent'
            env.setdefault('OTEL_SERVICE_NAME', f"agent-{_t}-{user_slug}")
        return env

    def _inject_user_mcp(self, env: dict, agent_type: str, user_slug: str,
                         owner_id: str | None = None, username: str | None = None) -> dict:
        """Merge the user's OWN running MCP-proxy endpoints into a resolved env.

        #36 gap 2 — per-user personal MCPs. Distinct from the stack-wide MCP
        registry, which mcp_config bakes into the static catalog at seed time
        (no user context). Here we pull THIS user's proxies from mcp-manager at
        launch time and merge per agent type:
          moltis        -> MOLTIS_MCP__SERVERS__* env keys (moltis maps the
                           double-underscore env to [mcp.servers.<id>])
          coding-tools  -> merged under the `mcp` key of OPENCODE_CONFIG_JSON
                           (preserving the stack-wide entries already there)
          hermes        -> RAZZFAZZ_USER_MCP_JSON env carrying the STRUCTURED
                           [{id,url}] specs as JSON; the hermes boot script parses
                           it in Python and execs `hermes mcp add` with a quoted
                           argv list (NO shell, NO eval).

        SECURITY (commit-review CRITICAL #2): the hermes wiring is passed as
        structured JSON DATA, never a shell-command string — the prior
        `eval`-based approach was a command-injection sink for cross-service /
        user-influenced ids/urls. ids are strict-validated upstream in
        agent_wiring; here we only carry data.

        Idempotent and only-own: the endpoint returns a single user's proxies,
        and re-running with the same proxy set produces the same env. Fail-safe:
        a missing/empty wiring leaves `env` unchanged.
        """
        # opencode/coding-tools and hermes/moltis were the original MCP consumers.
        # #36 follow-up: the sandboxed coding agents (codex + whatever runs Claude
        # Code / gsd-pi / pi) also consume per-user MCP. `codex` gets its own
        # ~/.codex/config.toml [mcp_servers.*] block; ALL coding-agent kinds get a
        # project /workspace/.mcp.json for Claude Code (installed by the user into
        # any coding-agent container). So the coding-agent kinds are wired too.
        _CODING_AGENT_KINDS = ("coding-tools", "opencode", "codex",
                               "gsd-pi", "user-defined")
        if agent_type not in (("moltis", "hermes") + _CODING_AGENT_KINDS):
            return env
        from app.services import mcp_manager_client
        # #2086: the owner id (and, at launch, the owner's username) lets
        # mcp-manager confirm the owner's sub against Authentik before it
        # wires the stack's gateway servers with the owner's agent token.
        wiring = mcp_manager_client.fetch_user_wiring(user_slug, owner_id=owner_id,
                                                      username=username)

        if agent_type == "moltis":
            env.update(wiring.get("moltis_env", {}))
        elif agent_type in _CODING_AGENT_KINDS:
            block = wiring.get("opencode_block", {})
            if block:
                raw = env.get("OPENCODE_CONFIG_JSON")
                try:
                    cfg = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    cfg = {}
                cfg.setdefault("mcp", {}).update(block)
                env["OPENCODE_CONFIG_JSON"] = json.dumps(cfg)
            # Claude Code .mcp.json (project-scoped): the entrypoint writes a
            # MANAGED block into /workspace/.mcp.json every boot, never clobbering
            # the user's manual `claude mcp add` entries. Carried as JSON DATA
            # (no shell) — same safety posture as the hermes specs.
            claude = wiring.get("claude_mcp", {}) or {}
            servers = claude.get("mcpServers") if isinstance(claude, dict) else None
            if servers:
                env["RAZZFAZZ_CLAUDE_MCP_JSON"] = json.dumps(servers)
            # Codex ~/.codex/config.toml [mcp_servers.*]: only for the codex kind.
            if agent_type == "codex":
                codex_block = wiring.get("codex_mcp", {}) or {}
                if codex_block:
                    env["RAZZFAZZ_CODEX_MCP_JSON"] = json.dumps(codex_block)
        elif agent_type == "hermes":
            specs = wiring.get("hermes_specs", [])
            if specs:
                # Structured JSON only — parsed + run with argv (no shell) in
                # the hermes boot script. Keep {id,url,headers}; headers carry
                # the per-proxy bearer (#61 CRITICAL-1) the boot script passes as
                # `--header`. headers omitted for a legacy bearer-less proxy.
                env["RAZZFAZZ_USER_MCP_JSON"] = json.dumps(
                    [{"id": s["id"], "url": s["url"],
                      **({"headers": s["headers"]} if s.get("headers") else {})}
                     for s in specs if s.get("id") and s.get("url")])
        return env

    def _mint_llm_manager_key(self, username: str, ref: str) -> dict | None:
        """#612: mint a per-user rzfz-sk key from llm-manager, or None.

        None on a gpustack box (no llm-manager container), on HTTP failure, or
        on any error — callers treat None as "keep catalog default env".
        The endpoint is peer-anchored (only the agent-manager container may
        call it), so no admin token is involved.
        """
        try:
            if self._docker.get_container_state('llm-manager') is None:
                return None
        except Exception:  # noqa: BLE001 — docker hiccup == not an LLM box
            return None
        try:
            import httpx
            resp = httpx.post(
                'http://llm-manager:8080/internal/agent-keys',
                json={'username': username, 'instance_id': ref},
                timeout=10)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("llm-manager key mint for %s failed: HTTP %s %s",
                           username, resp.status_code, resp.text[:200])
        except Exception as e:  # noqa: BLE001
            logger.warning("llm-manager key mint for %s failed: %s", username, e)
        return None

    #: Waits between revoke attempts. The manager answers 503 when it cannot
    #: clear its auth cache (and commits nothing), so a retry is safe.
    _REVOKE_BACKOFF_S = (1.0, 2.0)

    def _revoke_llm_manager_key(self, instance_config: dict,
                                instance: str = '') -> bool:
        """#612: revoke the instance's rzfz-sk on delete. Idempotent (a missing
        key on the manager side reports success). Returns True when revoked or
        nothing to revoke.

        #2562: the HTTP status is checked. A 5xx (the manager's "cache could
        not be invalidated, nothing changed") or a transport error is retried
        with a bounded backoff; if it still fails, a WARN names the instance
        and False is returned so the caller reports the revoke as failed —
        never as if it had worked."""
        kid = (instance_config or {}).get('_llm_manager_key_id')
        if not kid:
            return True
        import httpx
        waits = list(self._REVOKE_BACKOFF_S)
        attempts, why = 0, ''
        while True:
            attempts += 1
            retry = True
            try:
                resp = httpx.post(
                    f'http://llm-manager:8080/internal/agent-keys/{kid}/revoke',
                    timeout=10)
                if 200 <= resp.status_code < 300:
                    return True
                why = f"HTTP {resp.status_code} {(resp.text or '')[:200]}"
                retry = resp.status_code >= 500
            except Exception as e:  # noqa: BLE001
                why = str(e)
            if not retry or not waits:
                break
            time.sleep(waits.pop(0))
        logger.warning(
            "LLM key revoke for instance %s (key %s) FAILED after %d attempt(s): %s "
            "— the key may keep authenticating until the manager's auth cache "
            "expires (up to 5 minutes)", instance or '?', kid, attempts, why)
        return False

    @staticmethod
    def _usable_technical_names():
        """#2554: names of the live technical users bound to Gitea, or None
        when mcp-manager cannot be asked."""
        from app.services import mcp_manager_client
        users = mcp_manager_client.fetch_technical_users()
        if users is None:
            return None
        return {u.get('name') for u in mcp_manager_client.usable_technical_users(users)}

    def _technical_refusal(self, instance_config) -> str | None:
        """#2554: why a technical instance may not start now, or None."""
        if not _is_technical_config(instance_config):
            return None
        name = _technical_user_of(instance_config)
        names = self._usable_technical_names() if name else set()
        if names is None:
            return ('The MCP manager is not reachable, so the technical user cannot be checked. '
                    'Try again.')
        if name not in names:
            return (f'Technical user {name or "(none)"} is revoked or no longer bound to Gitea; '
                    'the agent stays stopped.')
        return None

    def _reconcile_technical_instances(self, instances) -> dict:
        """#2554 (operator decision 2026-10-14): the agent manager's own record
        of instance -> technical user (instance config `_technical_user`)
        decides; revoking the technical user stops every running instance
        bound to it. Nothing is stopped while mcp-manager cannot be asked."""
        out = {'technical_stopped': 0}
        # running ones (to stop) and stopped ones still holding an LLM key (a
        # revoke that failed on an earlier pass)
        rows = [r for r in instances if _is_technical_config(self._config_of(r))
                and (r.get('state') == 'running' or self._config_of(r).get('_llm_manager_key_id'))]
        if not rows:
            return out
        from app.services import mcp_manager_client
        users = mcp_manager_client.fetch_technical_users()
        if users is None:
            logger.warning("technical agents: mcp-manager unreachable; %d running technical "
                           "agent(s) not checked this pass", len(rows))
            return out
        if not users:
            # The mirror keeps revoked technical users (with revoked_at), so an
            # EMPTY listing is not "all revoked": mcp-manager lost its table
            # (a database reset or restore). Stopping on it would stop every
            # technical agent for nothing.
            logger.error("technical agents: mcp-manager lists NO technical users while %d technical "
                         "agent(s) run; not stopping any. If its database was reset, open "
                         "Settings > Technical users in the Configuration Portal to re-mirror them.",
                         len(rows))
            out['technical_listing_empty'] = True
            return out
        names = {u.get('name') for u in mcp_manager_client.usable_technical_users(users)}
        for row in rows:
            cfg = self._config_of(row)
            name = _technical_user_of(cfg)
            if name in names:
                continue
            if row.get('state') == 'running':
                try:
                    self.stop(row['id'], 'technical-user-revoked')
                    self._db.log_audit(row.get('user_id'), 'technical_user_revoked_stop',
                                       row.get('agent_type'), row['id'])
                    out['technical_stopped'] += 1
                    logger.warning("technical agents: stopped %s, technical user %s is revoked "
                                   "or no longer bound to Gitea", row.get('container_name'), name)
                except Exception:  # noqa: BLE001
                    logger.exception("technical agents: could not stop %s", row.get('container_name'))
                    continue
            # its LLM key dies with it: a stopped container's key is still a
            # credential (the next recreate after a re-enable mints a new one)
            if cfg.get('_llm_manager_key_id'):
                if self._revoke_llm_manager_key(cfg, instance=row.get('container_name') or ''):
                    self._db.merge_instance_config(row['id'], {'_llm_manager_key': None,
                                                               '_llm_manager_key_id': None})
                else:
                    logger.error("technical agents: the LLM key of %s could not be revoked; "
                                 "retried on the next pass", row.get('container_name'))
        return out

    def _gitea_credentials(self, username: str, user_slug: str, user_groups,
                           user_config) -> dict:
        """#2554: the git identity of a sandboxed agent, as instance_config
        keys. Personal (the default): the owner's own minted token. Technical:
        the technical user's Gitea service user, for a caller the role
        catalogue grants agent-manager/admin (#2473) only; no group name is
        written here. Raises PermissionError instead of falling back."""
        uc = user_config or {}
        if _identity_mode(uc) == 'technical':
            if not allows('agent-manager', 'admin', list(user_groups or [])):
                raise PermissionError('only an admin may launch an agent as a technical user')
            name = uc.get('technical_user')
            if not isinstance(name, str) or not TECHNICAL_NAME_RE.fullmatch(name):
                raise PermissionError('technical mode needs a technical user (svc-...)')
            from app.services import mcp_manager_client
            got = mcp_manager_client.fetch_technical_git(name)
            if not got.get('token'):
                raise PermissionError(f'technical user {name} is not bound to Gitea for agents, '
                                      'or has no Gitea service user yet')
            return {'_gitea_token': got['token'], '_git_name': got['name'],
                    '_git_email': got['email'], '_technical_user': name}
        return self._mint_gitea_for_instance(username, user_slug, {})

    def _mint_gitea_for_instance(self, username: str, user_slug: str,
                                 instance_config: dict) -> dict:
        """#165 + #2554: mint the instance's Gitea token and record, next to
        it, its name and owner (for the revoke on delete) and the Gitea
        user's name and email (the agent's git identity). Without a token
        nothing else is recorded and the slug identity applies."""
        name = f"coding-agent-{user_slug}-{secrets.token_hex(3)}"
        tok = self._mint_gitea_token(username, user_slug, token_name=name)
        instance_config['_gitea_token'] = tok
        if tok:
            instance_config['_gitea_token_name'] = name
            instance_config['_gitea_user'] = username
            git_name, git_email = self._gitea_identity(tok)
            if git_name and git_email:
                instance_config['_git_name'] = git_name
                instance_config['_git_email'] = git_email
        return instance_config

    def _gitea_identity(self, token: str) -> tuple[str, str]:
        """#2554: (name, email) of the Gitea user the token belongs to, so the
        agent's commits are attributed to that user in Gitea (Gitea matches a
        commit by its author email). ('', '') on any failure or an unusable
        value; the slug fallback then applies."""
        if not token:
            return '', ''
        try:
            import httpx
            r = httpx.get(f"{_gitea_internal_url()}/api/v1/user",
                          headers={"Authorization": f"token {token}"}, timeout=5,
                          follow_redirects=False)
            if r.status_code != 200:
                return '', ''
            u = r.json()
            if not isinstance(u, dict):
                return '', ''
            name = u.get("full_name") or u.get("login") or ''
            email = u.get("email") or ''
            if not isinstance(name, str) or not isinstance(email, str):
                return '', ''
            # env values and `git config` input: no control characters, and
            # no braces (never a placeholder, even in a future template path)
            if re.search(r'[\x00-\x1f\x7f{}]', name + email) or '@' not in email:
                return '', ''
            return name.strip(), email.strip()
        except Exception:  # noqa: BLE001 — identity is best-effort
            logger.warning("gitea identity lookup failed", exc_info=False)
            return '', ''

    def _revoke_gitea_token(self, username: str, token_name: str) -> None:
        """#2554: delete exactly this instance's Gitea token. Gitea's token
        API takes Basic auth of the user or a site admin; the box admin's
        credentials come from the stack env. Best-effort and logged."""
        if not token_name or not username:
            return
        auth = (os.environ.get('GITEA_ADMIN_USER', ''), os.environ.get('GITEA_ADMIN_PASSWORD', ''))
        if not all(auth):
            logger.warning("gitea token %s not revoked: GITEA_ADMIN_USER/PASSWORD unset", token_name)
            return
        try:
            import httpx
            from urllib.parse import quote
            base = f"{_gitea_internal_url()}/api/v1/users/{quote(username, safe='')}/tokens"
            # the listing is paged: read page after page until an empty one
            for page in range(1, 101):
                r = httpx.request("GET", base, auth=auth, timeout=10, follow_redirects=False,
                                  params={"page": page, "limit": 50})
                tokens = r.json() if r.status_code == 200 else None
                if not isinstance(tokens, list):
                    logger.warning("gitea token %s not revoked: listing HTTP %s",
                                   token_name, r.status_code)
                    return
                if not tokens:
                    break
                for t in tokens:
                    if isinstance(t, dict) and t.get("name") == token_name and isinstance(t.get("id"), int):
                        d = httpx.request("DELETE", f"{base}/{t['id']}", auth=auth, timeout=10,
                                          follow_redirects=False)
                        if d.status_code not in (204, 404):
                            logger.warning("gitea token %s revoke: HTTP %s", token_name, d.status_code)
                        return
            logger.warning("gitea token %s not revoked: not found in %s's token list",
                           token_name, username)
        except Exception:  # noqa: BLE001
            logger.warning("gitea token %s revoke failed", token_name, exc_info=True)

    def _mint_gitea_token(self, username: str, user_slug: str,
                          token_name: str | None = None) -> str:
        """#165: best-effort mint of a PER-USER Gitea access token via the gitea
        admin CLI (`gitea admin user generate-access-token`), run with docker
        exec through the socket-proxy — the same EXEC path _run_pre_stop uses.

        PER-USER (never the shared box admin token) so one user's sandbox cannot
        reach a peer's repos. Returns '' (non-fatal) when:
          * the `gitea` profile is inactive (no gitea container),
          * the user hasn't logged into Gitea yet (OIDC auto-provision — the CLI
            errors "user does not exist"), or
          * the CLI otherwise errors.
        In every '' case the coding-agent entrypoint skips the credential block;
        the user can still clone PUBLIC repos via the insteadOf rewrite and clone
        PRIVATE repos by pasting their own PAT in the web-UI "Clone from Gitea"
        flow (which GITEA_EXTERNAL_URL now surfaces).
        """
        profiles = [p.strip()
                    for p in os.environ.get('COMPOSE_PROFILES', '').split(',')]
        if 'gitea' not in profiles:
            return ''
        # Unique-per-instance token name so a re-provision never collides with a
        # leftover token from a previously-deleted instance of the same user.
        token_name = token_name or f"coding-agent-{user_slug}-{secrets.token_hex(3)}"
        cmd = ['su-exec', 'git', 'gitea', 'admin', 'user',
               'generate-access-token', '--username', username,
               '--token-name', token_name, '--raw',
               '--scopes', 'write:repository,read:user,read:organization']
        try:
            rc, out = self._docker.exec_in_container('gitea', cmd, timeout=15)
        except Exception as e:  # noqa: BLE001
            logger.warning("gitea token mint for %s failed: %s", username, e)
            return ''
        text = (out.decode('utf-8', 'replace')
                if isinstance(out, (bytes, bytearray)) else str(out or '')).strip()
        if rc != 0:
            if 'does not exist' in text.lower():
                # #218: on a box whose OIDC source was created without
                # `--username`, Gitea named the auto-provisioned account after
                # the Authentik `sub` (a hex UUID), so the readable name really
                # is absent. Say so — the generic "skipping" line below sent
                # operators looking for a permission problem that isn't there.
                #
                # Deliberately NOT resolved by searching Gitea for a
                # similar-looking account: this token grants write access to
                # that user's repos, and the only identifier this call has is
                # the Authentik username. Matching anything fuzzier would risk
                # minting one user's token against another user's account —
                # precisely what per-user (rather than shared-admin) tokens
                # exist to prevent. The retrofit in modules/gitea/init-gitea.sh
                # fixes new logins; existing hex accounts need an admin rename.
                logger.warning(
                    "gitea token mint for %s: no such Gitea user. Either the "
                    "user has not logged into Gitea yet, or this box predates "
                    "#218 and their account is named after the OIDC 'sub' "
                    "(hex). Check `gitea admin user list`; if the name is hex, "
                    "rename it to the Authentik username. Falling back to the "
                    "UI 'Clone from Gitea' PAT flow.", username)
            else:
                logger.info("gitea token mint for %s rc=%s (%s) — skipping "
                            "(user can paste a PAT in the UI)",
                            username, rc, text[:120])
            return ''
        # `--raw` prints ONLY the token; take the last non-empty line + sanity-check.
        tok = ''
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line:
                tok = line
                break
        return tok if tok and ' ' not in tok and len(tok) <= 100 else ''

    def _db_role_owners(self, instance_id, db_user: str) -> list[str]:
        """#1039: the OTHER live instances whose database resolves to `db_user`
        (recorded names, else their own #1988 formula — what their recreate
        connects with)."""
        owners = []
        for row in self._db.list_live_db_instance_rows() or []:
            if str(row.get('id')) == str(instance_id):
                continue
            _, user = db_identity_of(_parse_config(row.get('config')),
                                     row.get('agent_type') or '', row.get('user_slug') or '',
                                     row.get('instance_no') or 1)
            if user == db_user:
                owners.append(str(row.get('container_name') or row.get('id')))
        return owners

    def _db_role_connections(self, instance_id, container_name, db_user: str):
        """#1039 review I2/I3: who ACTUALLY connects as `db_user` — read from
        every agent container's DATABASE_URL, not from the rows (before #2529,
        instance #N ran on instance #1's role, and no row says so).

        Returns `(own_passwords, foreign_containers)`: the passwords this
        instance's own containers (by instance label, else exact name) connect with, and the names
        of any other container that connects as `db_user`. Raises when docker
        cannot be read: "cannot tell" is never "nobody"."""
        own, foreign = set(), []
        for c in self._docker.agent_container_envs():
            ident = db_identity_from_env(c.get('env'))
            if not ident or ident[1] != db_user:
                continue
            name = c.get('name') or ''
            # The instance label decides; only an unlabelled container is
            # matched by name — exactly, never by prefix: instance #2 of the
            # same user is `<#1's container_name>-2`.
            label = str(c.get('instance_id') or '')
            mine = (label == str(instance_id)) if label else bool(
                container_name and name == container_name)
            if not mine:
                foreign.append(name)
                continue
            pw = db_password_from_env(c.get('env'), db_user)
            if pw:
                own.add(pw)
        return own, foreign

    def ensure_db_password(self, instance_id, instance_config: dict, db_user: str,
                           container_name: str | None = None) -> bool:
        """#1039 (reopened): a requires_db row created before AGM-12 carries no
        `_db_password`. Recreated from it, DATABASE_URL has an empty password
        and the container restart-loops on 28P01. Set a password on the role,
        then persist it. True iff this call repaired the row; False leaves the
        row without a password and the caller must not recreate from it.

        `db_user` is the role the next recreate connects with (`db_identity_of`).
        The password is the one this instance's container already connects
        with, when there is one — the repair never locks a healthy container
        out — else a minted one. Left alone, and logged:
          * a role another live ROW resolves to, or another CONTAINER connects
            as (setting it would lock that instance out);
          * docker unreadable — a container might use the role; retried later;
          * the instance's own containers disagree on the password, or carry
            one that `_resolve_env` could not render back unencoded;
          * a role that does not exist (nothing to repair into).

        Concurrent passes (post-install runs create_app() in extra processes
        while the manager boots) are serialised by a session advisory lock on
        the role, held across re-reading the row, ALTER and persist: a second
        pass finds the first one's password on the row and takes it. The
        password is never logged."""
        if instance_config.get('_db_password'):
            return False
        owners = self._db_role_owners(instance_id, db_user)
        if owners:
            logger.warning("#1039: role %s is also the database role of %s — %s keeps "
                           "no password; resolve by hand", db_user, ', '.join(owners),
                           instance_id)
            return False
        try:
            own, foreign = self._db_role_connections(instance_id, container_name, db_user)
        except Exception:  # noqa: BLE001 — cannot tell who uses the role
            logger.warning("#1039: docker could not be read — %s (role %s) keeps no "
                           "password for now; retried on the next start or recreate",
                           instance_id, db_user, exc_info=True)
            return False
        if foreign:
            logger.warning("#1039: role %s is in use by container(s) %s, which do not "
                           "belong to %s — no password set; resolve by hand",
                           db_user, ', '.join(sorted(foreign)), instance_id)
            return False
        if len(own) > 1:
            logger.warning("#1039: the containers of %s connect as %s with different "
                           "passwords — none is set; resolve by hand", instance_id, db_user)
            return False
        known = next(iter(own), None)
        if known is not None and not _URL_SAFE_PASSWORD.fullmatch(known):
            logger.warning("#1039: the password %s's container uses for %s would not "
                           "survive a recreate unencoded — none is set; resolve by hand",
                           instance_id, db_user)
            return False
        from psycopg2 import sql as _sql
        admin_dsn = self._config['DATABASE_URL'].rsplit('/', 1)[0] + '/postgres'
        conn = psycopg2.connect(admin_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (f'rzfz-1039:{db_user}',))
            # Another pass may have repaired the row while this one waited.
            current = _parse_config((self._db.get_instance(instance_id) or {}).get('config'))
            if current.get('_db_password'):
                instance_config['_db_password'] = current['_db_password']
                logger.info("#1039: %s was repaired concurrently; taking that password",
                            instance_id)
                return False
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (db_user,))
            if not cur.fetchone():
                logger.warning("#1039: role %s does not exist — %s keeps no password",
                               db_user, instance_id)
                return False
            db_pass = known or secrets.token_urlsafe(24)
            # #2586: same statement shape as _create_instance_db — and, like
            # it, only the SCRAM verifier is sent, never the plaintext, so a
            # cluster with log_statement=ddl|all cannot log the password.
            cur.execute(_sql.SQL('ALTER USER {} WITH PASSWORD %s').format(_sql.Identifier(db_user)),
                        (scram_sha256_verifier(db_pass),))
            self._db.merge_instance_config(instance_id, {'_db_password': db_pass})
            instance_config['_db_password'] = db_pass
        finally:
            cur.close()
            conn.close()          # also releases the advisory lock
        logger.info("#1039: set and persisted the missing database password of %s (%s; %s)",
                    db_user, instance_id,
                    'kept the one its container connects with' if known else 'minted')
        return True

    def _create_instance_db(self, agent_type: str, user_slug: str,
                            instance_config: dict, instance_no: int = 1):
        """Create a per-instance PostgreSQL database and user."""
        import os
        db_name, db_user = instance_db_identity(agent_type, user_slug, instance_no)
        db_pass = secrets.token_urlsafe(24)
        # #2529: the names are minted here, once, and persisted by the caller
        # with the password (launch → update_instance_config). Nothing later
        # recomputes them.
        instance_config['_db_name'] = db_name
        instance_config['_db_user'] = db_user
        instance_config['_db_password'] = db_pass

        # Connect to postgres database (admin)
        admin_dsn = self._config['DATABASE_URL'].rsplit('/', 1)[0] + '/postgres'
        conn = psycopg2.connect(admin_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}"')
                logger.info(f"Created database {db_name}")
            cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = %s", (db_user,))
            if not cur.fetchone():
                cur.execute(f"CREATE USER \"{db_user}\" WITH PASSWORD %s",
                            (scram_sha256_verifier(db_pass),))
                cur.execute(f'GRANT ALL PRIVILEGES ON DATABASE "{db_name}" TO "{db_user}"')
                logger.info(f"Created user {db_user}")
            else:
                # rc6.7 #72: re-provisioning a previously-destroyed instance
                # generates a fresh `_db_password` and hands it to the new
                # container via DATABASE_URL, but the existing postgres
                # role still carries the OLD password — auth fails with
                # `password authentication failed for user
                # "agent_paperclip_<slug>"` and the container restart-loops.
                # Sync the role's password to the freshly generated one.
                # Existing per-instance DB data is preserved (no DROP).
                cur.execute(f"ALTER USER \"{db_user}\" WITH PASSWORD %s",
                            (scram_sha256_verifier(db_pass),))
                logger.info(f"Reset password for existing user {db_user}")
            # Grant schema-level permissions (required for CREATE TABLE etc.)
            conn.close()
            conn = psycopg2.connect(admin_dsn.rsplit('/', 1)[0] + f'/{db_name}')
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(f'GRANT ALL ON SCHEMA public TO "{db_user}"')
            cur.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{db_user}"')
            cur.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{db_user}"')
            logger.info(f"Granted schema permissions to {db_user}")
        finally:
            cur.close()
            conn.close()

    def _drop_instance_db(self, db_name: str, db_user: str):
        """Drop a per-instance database and its role — exactly the names the
        instance was created with (#2529: never recomputed from the row)."""
        from psycopg2 import sql as _sql
        admin_dsn = self._config['DATABASE_URL'].rsplit('/', 1)[0] + '/postgres'
        conn = psycopg2.connect(admin_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(_sql.SQL('DROP DATABASE IF EXISTS {}').format(_sql.Identifier(db_name)))
            cur.execute(_sql.SQL('DROP USER IF EXISTS {}').format(_sql.Identifier(db_user)))
            logger.info("Dropped database %s and user %s", db_name, db_user)
        finally:
            cur.close()
            conn.close()
