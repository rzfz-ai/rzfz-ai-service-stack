# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Provisioning engine — creates and manages per-user agent instances."""

import json
import logging
import os
import re
import secrets

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

logger = logging.getLogger(__name__)

# #36 security-review — the socket-less coding-agent types that run
# arbitrary/LLM-driven code and MUST be sandboxed (egress-controlled
# `coding-agents` net only, cap_drop ALL, read-only root + tmpfs, pids_limit,
# no host bind-mounts, no docker socket). Kept as an authoritative constant
# here (rather than a DB column) so enforcement is migration-free and cannot
# be weakened by a stale/partial DB row: a type is sandboxed iff it's in this
# set, regardless of what the DB carries. openhands/hermes/moltis/paperclip are
# deliberately NOT here — they keep their existing (unsandboxed) wiring.
SANDBOXED_TYPES = frozenset({'opencode', 'gsd-pi', 'codex', 'user-defined'})

# W4a (#256) — socket-less assistant classes fenced onto the egress-controlled
# `agent-assistants` net (gpustack + agent-manager join it; postgres / valkey /
# authentik / config-portal stay unreachable). Deliberately NOT here:
#   * moltis / openhands — they hold the docker socket; a network fence around a
#     socket holder is theater (the socket is the risk, tracked in #257/#256).
#   * paperclip — needs postgres+valkey directly (per-instance DB creds bound
#     the blast radius instead).
# Same migration-free authority pattern as SANDBOXED_TYPES.
ASSISTANT_NET_TYPES = frozenset({'hermes'})

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
# `_llm_manager_key_id`) and the server-resolved `mem_limit`.
LAUNCH_CONFIG_ALLOWED_KEYS = frozenset({'mem_gb', 'custom_name', 'pids_limit', 'new_instance'})


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

    return cfg, None


# #959 D1: widened from #612's OPENAI_*/GPUSTACK_* pair to also cover the
# LLM_BASE_URL/LLM_EMBEDDING_BASE_URL (+ their *_API_KEY) agents whose
# catalog env never went through the OpenAI/GPUStack-shaped names.
_LLM_BASE_ENV_KEYS = ('OPENAI_BASE_URL', 'GPUSTACK_BASE_URL', 'LLM_BASE_URL', 'LLM_EMBEDDING_BASE_URL')
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
        if new_v != v:
            env[k] = new_v


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
    opencode, codex, user-defined, coding-tools, moltis, openhands and
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
    def __init__(self, db, docker_client, caddy_client, catalog, config: dict,
                 authentik_client=None):
        # #516: per-host consecutive-failure backoff for Authentik registration
        self._authentik_backoff = {}
        self._db = db
        self._docker = docker_client
        self._caddy = caddy_client
        self._catalog = catalog
        self._config = config
        self._authentik = authentik_client

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

    def check_quota(self, user_slug, user_groups: list[str],
                    agent_type: str) -> tuple[bool, str]:
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
        type_count = self._db.count_user_instances(user_slug, agent_type=agent_type)
        if type_count >= tier['max_per_type']:
            return False, f'You already have {type_count} {type_info["display_name"]} instance(s) (max {tier["max_per_type"]}).'

        # Check heavy tier limit
        if type_info['tier'] == 'heavy':
            heavy_count = self._db.count_user_instances(user_slug, tier='heavy')
            if heavy_count >= tier['max_heavy']:
                return False, f'You have reached your heavy agent limit ({tier["max_heavy"]}).'

        # #959/W3: per-user max-RUNNING-instances cap, enforced (not just
        # displayed). None/absent = unlimited, matching max_per_type/max_heavy.
        # This is a separate axis from max_per_type (total non-destroyed
        # instances of one type) — it caps how many instances of ANY type may
        # be simultaneously running.
        max_running = tier.get('max_running')
        if max_running is not None:
            running_count = self._db.count_user_running_instances(user_slug)
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

    def check_memory_budget(self, user_slug: str, mem_limit: str,
                            exclude_instance_mb: int = 0) -> tuple[bool, str]:
        """L2 + L3 — refuse a launch/increase that would exceed the global
        budget or the per-user cap. `exclude_instance_mb` subtracts the current
        allocation of the instance being resized (so a raise counts only the
        DELTA against the sums). Returns (allowed, reason)."""
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
            user_running = int(self._db.sum_user_running_mem_mb(user_slug)) - int(exclude_instance_mb)
            if user_running + want_mb > cap:
                return False, (
                    'Your personal agents memory cap would be exceeded — stop '
                    f'one of your agents first (cap {cap} MB, you are using '
                    f'{max(0, user_running)} MB, requested {want_mb} MB).'
                )
        return True, 'OK'

    def launch(self, agent_type: str, user_id: str, username: str,
               user_groups: list[str], user_config: dict = None) -> tuple[str | None, str]:
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
        existing = self._db.get_instance_by_type_and_user(agent_type, lookup_slugs)
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
                return self.start(existing['id'], username, user_groups)
            return str(existing['id']), f'Instance already {existing["state"]}.'

        # Quota check (AGM-8 — over both slug candidates, see above)
        allowed, reason = self.check_quota(lookup_slugs, user_groups, agent_type)
        if not allowed:
            return None, reason

        type_info = self._catalog.get_type(agent_type)
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
        ok, mem_reason = self.check_memory_budget(user_slug, mem_limit)
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
        if agent_type in SANDBOXED_TYPES:
            instance_config['_gitea_token'] = self._mint_gitea_token(
                username, user_slug)

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
        if _type_uses_llm_endpoint(type_info):
            minted = self._mint_llm_manager_key(username, container_name)
            if minted:
                instance_config['_llm_manager_key'] = minted['key']
                instance_config['_llm_manager_key_id'] = minted['id']
        # Persist the chosen limit so it survives a recreate (upgrade() re-reads
        # config) and so the budget sums see the real per-instance allocation.
        instance_config['mem_limit'] = mem_limit
        instance_id = self._db.create_instance(
            agent_type, user_id, user_slug, container_name, instance_config,
            instance_no=instance_no
        )

        try:
            # Create volumes
            volumes = {}
            for vol_spec in (json.loads(type_info['volumes'])
                             if isinstance(type_info['volumes'], str)
                             else type_info['volumes']):
                # rc6.7 #91: support `host_path` bind mounts in addition to
                # named volumes. Used by openhands to bind the monkey-patch
                # script from STACK_HOST_PATH so the per-user backend can
                # apply the same readiness-probe rewrite the global compose
                # uses. Templated like other catalog values.
                if 'host_path' in vol_spec:
                    host_path = (vol_spec['host_path']
                                 .replace('{{STACK_HOST_PATH}}',
                                          os.environ.get('STACK_HOST_PATH', '')))
                    volumes[host_path] = vol_spec['mount']
                else:
                    vol_name = f"agent-{agent_type}-{user_slug}{sfx}-{vol_spec['name_suffix']}"
                    self._docker.create_volume(vol_name)
                    volumes[vol_name] = vol_spec['mount']

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
                                    instance_id=instance_id)
            # #36 gap 2 — merge the user's OWN running MCP-proxy endpoints.
            env = self._inject_user_mcp(env, agent_type, user_slug)

            # Create container
            ports = (json.loads(type_info['ports'])
                     if isinstance(type_info['ports'], str) else type_info['ports'])
            labels = {
                'razzfazz.managed': 'true',
                'razzfazz.agent.type': agent_type,
                'razzfazz.agent.user': user_slug,
                'razzfazz.agent.instance': str(instance_id),
            }

            container_id = self._docker.create_container(
                name=container_name,
                image=type_info['image'],
                version=type_info['version'],
                environment=env,
                volumes=volumes,
                # #36 / PR #84 — the tier-gated per-instance mem_limit (persisted
                # on instance_config above), not the catalog default.
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
                # the env vars follow.
                command=_resolve_command_llm_placeholders(
                    type_info.get('command'), instance_config.get('_llm_manager_key')) or None,
                entrypoint=type_info.get('entrypoint') or None,
                # #36 security-review — sandbox the coding-agent split types.
                sandbox=_is_sandboxed(agent_type, type_info),
                assistant_net=_is_assistant_net(agent_type),
                coding_net=_is_coding_net(agent_type),
            )

            # Start container
            self._docker.start_container(container_id)

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
                companion_env = self._resolve_env(
                    type_info, user_slug, username, instance_config,
                    template_field='companion_env_template',
                    instance_id=instance_id,
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
            self._db.update_instance_state(
                instance_id, 'running',
                container_id=container_id,
                image_version=type_info['version'],
            )
            self._db.log_audit(user_id, 'launch', agent_type, instance_id,
                               {'container': container_name,
                                'image_version': type_info['version']})

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
        built or pulled on this box) is the common case for openhands /
        paperclip when their profile is off — surface an actionable message
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

    #: #1143 — container states that POSITIVELY mean "this instance is not
    #: running". Absence (`get_container_state` -> None) is handled alongside
    #: them. Deliberately an allow-list of DEAD states rather than "anything
    #: that isn't 'running'": 'restarting' is on its way up, 'paused' is up and
    #: frozen, 'removing' is mid-teardown — demoting any of those would let a
    #: second container be launched onto the live one's volumes, which is far
    #: worse than telling the user to retry. An unknown/future docker state
    #: falls through the same way, so the guard fails CLOSED.
    DEAD_CONTAINER_STATES = frozenset({'exited', 'dead', 'created'})

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

    def start(self, instance_id, username: str,
              user_groups: list[str] | None = None) -> tuple[str, str]:
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
        # #1143: ask docker before believing the row. Without this the guard
        # below shadowed the A1 self-heal a few lines down — a container that
        # had been removed externally could never be reached, because the row
        # still said 'running' and start() returned before it ever looked.
        instance = self._reconcile_instance_state(instance)
        if instance['state'] == 'running':
            return str(instance_id), 'Already running.'

        if user_groups is not None:
            tier = self._db.resolve_user_tier(user_groups)
            max_running = (tier or {}).get('max_running')
            if max_running is not None:
                running_count = self._db.count_user_running_instances(instance['user_slug'])
                if running_count >= max_running:
                    return None, (f'You already have {running_count} agent(s) running '
                                   f'(max {max_running}). Stop one first.')

        type_info = self._catalog.get_type(instance['agent_type'])

        # A1 self-heal: if the container is gone, run upgrade() which will
        # see the missing-container case (its remove step is already
        # tolerant of NotFound), then re-create with the current catalog.
        if self._docker.get_container_state(instance['container_name']) is None:
            logger.warning(
                f"start: container {instance['container_name']} missing — "
                f"falling back to upgrade()/recreate (A1 self-heal)"
            )
            return self.upgrade(instance_id, username)

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

            self._db.update_instance_state(instance_id, 'running')
            self._db.log_audit(instance['user_id'], 'start', instance['agent_type'], instance_id)
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

        ok, reason = self.check_memory_budget(
            instance['user_slug'], new_mem, exclude_instance_mb=current_mb)
        if not ok:
            return None, reason

        # Persist first so a recreate (fallback) reads the new value.
        cfg['mem_limit'] = new_mem
        self._db.update_instance_config(instance_id, cfg)

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
                    instance_id, {'mem_limit': new_mem, 'method': 'recreate'})
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
                        name: str) -> tuple[str | None, str]:
        """#233 — set (or clear) an instance's user-chosen name.

        `name` is expected ALREADY validated by `validate_custom_name` — the
        caller needs to distinguish "invalid input" (400) from "not yours"
        (404), and that distinction belongs at the API edge.

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

        cfg = self._load_config(instance)
        if name:
            cfg['custom_name'] = name
        else:
            cfg.pop('custom_name', None)
        self._db.update_instance_config(instance_id, cfg)
        self._db.log_audit(instance.get('user_id'), 'rename',
                           instance['agent_type'], instance_id,
                           {'custom_name': name or None})

        if not name:
            return str(instance_id), 'Name cleared — showing the agent type again.'
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

        cfg = self._load_config(instance)
        cfg['pids_limit'] = int(pids_limit)
        self._db.update_instance_config(instance_id, cfg)

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

    def repair_instance(self, instance_id, username: str | None = None) -> dict | None:
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
                                    username or instance['user_slug'])
            result['recreated'] = bool(iid) and 'failed' not in msg.lower()
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
        # PR #84 C1: deregister the per-instance Authentik provider on stop
        # (re-registered on start). Keeps the outpost's provider list tidy.
        self._deregister_authentik(instance['agent_type'], instance_id)
        self._db.update_instance_state(instance_id, 'stopped')
        self._db.log_audit(instance['user_id'], 'stop', instance['agent_type'], instance_id)
        return str(instance_id), f'{instance["type_display_name"]} stopped.'

    def restart(self, instance_id, username: str) -> tuple[str | None, str]:
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

    def upgrade(self, instance_id, username: str,
                target_version: str | None = None,
                force: bool = False) -> tuple[str | None, str]:
        """M030-S2: in-place upgrade of an instance to a new image version.

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
             flush, openhands runtime drain, etc.) — best-effort, timeboxed
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

        type_info = self._catalog.get_type(instance['agent_type'])
        if not type_info:
            return str(instance_id), f'Catalog type {instance["agent_type"]!r} not found — cannot upgrade.'

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
                if not stale:
                    return str(instance_id), f'Already on {new_version}; nothing to do.'
                logger.info(
                    "upgrade: %s tag %s unchanged but image rebuilt "
                    "(container %s → tag %s); recreating onto the new image.",
                    instance['container_name'], new_version,
                    (running_id or '')[:19], (tagged_id or '')[:19])

        # 1. Pre-stop hook (Q6) — clean-shutdown the runtime before docker stop.
        self._run_pre_stop(instance, type_info)

        # 2. Stop + remove containers (companion first to avoid dangling reference).
        #    CRITICAL: remove_container without -v so named volumes survive.
        companion = self._companion_name(instance, type_info)
        if companion:
            try:
                self._docker.remove_container(companion)
            except Exception as ce:
                logger.warning(f"upgrade: failed to remove companion {companion}: {ce}")
        try:
            self._docker.remove_container(instance['container_name'])
        except Exception as e:
            logger.exception(f"upgrade: failed to remove primary {instance['container_name']}")
            self._db.update_instance_state(instance_id, 'error', error_message=f'remove failed: {e}')
            return str(instance_id), f'Upgrade failed during stop: {e}'


        # 3. Re-create with new image. Volumes get re-created by name
        #    (idempotent) and Docker reattaches them.
        try:
            volumes = {}
            for vol_spec in (json.loads(type_info['volumes'])
                             if isinstance(type_info['volumes'], str)
                             else type_info['volumes']):
                if 'host_path' in vol_spec:
                    host_path = (vol_spec['host_path']
                                 .replace('{{STACK_HOST_PATH}}',
                                          os.environ.get('STACK_HOST_PATH', '')))
                    volumes[host_path] = vol_spec['mount']
                else:
                    vol_name = (f"agent-{instance['agent_type']}-"
                                f"{instance['user_slug']}{instance_suffix(instance.get('instance_no'))}-{vol_spec['name_suffix']}")
                    self._docker.create_volume(vol_name)  # idempotent
                    volumes[vol_name] = vol_spec['mount']

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
            if (not instance_config.get('_llm_manager_key')
                    and _type_uses_llm_endpoint(type_info)):
                minted = self._mint_llm_manager_key(
                    username, instance['container_name'])
                if minted:
                    instance_config['_llm_manager_key'] = minted['key']
                    instance_config['_llm_manager_key_id'] = minted['id']
                    try:
                        self._db.update_instance_config(instance_id, instance_config)
                    except Exception:  # noqa: BLE001 — env still gets the key
                        logger.warning("upgrade: could not persist the minted "
                                       "llm-manager key for %s",
                                       instance['container_name'], exc_info=True)
            # #36 / PR #84 — recreate with the SAME persisted per-instance
            # mem_limit (a power user's raised memory survives an upgrade);
            # fall back to the catalog default for pre-governance instances.
            recreate_mem = instance_config.get('mem_limit') or type_info['mem_limit']
            env = self._resolve_env(type_info, instance['user_slug'], username, instance_config,
                                    instance_id=instance['id'])
            # #36 gap 2 — re-merge the user's MCP-proxy endpoints on relaunch
            # (picks up MCPs connected since the last launch).
            env = self._inject_user_mcp(env, instance['agent_type'], instance['user_slug'])
            ports = (json.loads(type_info['ports'])
                     if isinstance(type_info['ports'], str) else type_info['ports'])
            labels = {
                'razzfazz.managed': 'true',
                'razzfazz.agent.type': instance['agent_type'],
                'razzfazz.agent.user': instance['user_slug'],
                'razzfazz.agent.instance': str(instance_id),
            }

            container_id = self._docker.create_container(
                name=instance['container_name'],
                image=type_info['image'],
                version=new_version,
                environment=env,
                volumes=volumes,
                mem_limit=recreate_mem,
                cpu_limit=type_info['cpu_limit'],
                # #232 — the per-INSTANCE PID cap when the user set one,
                # else the per-type default (#221, coding family = 2048).
                # None falls back to docker_client's 512 fork-bomb floor.
                pids_limit=resolve_pids_limit(instance_config, type_info),
                docker_socket=type_info['requires_docker_socket'],
                labels=labels,
                # #959 D2: resolve {{LLM_BASE_URL}}/{{LLM_API_KEY}} placeholders
                # (e.g. Hermes' bootstrap) against the same minted-key switch
                # the env vars follow, on recreate too.
                command=_resolve_command_llm_placeholders(
                    type_info.get('command'), instance_config.get('_llm_manager_key')) or None,
                entrypoint=type_info.get('entrypoint') or None,
                # #36 security-review — keep the sandbox on upgrade/recreate too.
                sandbox=_is_sandboxed(instance['agent_type'], type_info),
                assistant_net=_is_assistant_net(instance['agent_type']),
                coding_net=_is_coding_net(instance['agent_type']),
            )
            self._docker.start_container(container_id)

            # Companion (e.g. hermes-workspace) — same dance with new version.
            if type_info.get('companion_image'):
                companion_suffix = type_info.get('companion_suffix') or 'workspace'
                companion_name = f"{instance['container_name']}-{companion_suffix}"
                companion_volumes = {}
                for vol_spec in (json.loads(type_info.get('companion_volumes') or '[]')
                                 if isinstance(type_info.get('companion_volumes'), str)
                                 else (type_info.get('companion_volumes') or [])):
                    vol_name = (f"agent-{instance['agent_type']}-"
                                f"{instance['user_slug']}{instance_suffix(instance.get('instance_no'))}-{vol_spec['name_suffix']}")
                    self._docker.create_volume(vol_name)
                    companion_volumes[vol_name] = vol_spec['mount']
                companion_env = self._resolve_env(
                    type_info, instance['user_slug'], username, instance_config,
                    template_field='companion_env_template',
                    instance_id=instance['id'],
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
                self._docker.start_container(companion_id)

            # #606: no Caddy route to re-register — the static wildcard
            # carries the instance host across the recreate unchanged.
            # PR #84 C1: (re)register the per-instance Authentik provider after
            # the upgrade recreate (idempotent — the host/token is unchanged).
            self._register_authentik(instance['agent_type'], instance_id)

            # 4. Mark upgraded — image_version + last_upgraded_at + state running.
            self._db.update_instance_state(
                instance_id, 'running',
                container_id=container_id, image_version=new_version,
            )
            self._db.mark_instance_upgraded(instance_id, new_version)
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

        # Stop and remove containers (companion first to avoid dangling reference)
        companion = self._companion_name(instance, type_info)
        if companion:
            try:
                self._docker.remove_container(companion)
            except Exception as ce:
                logger.warning(f"Failed to remove companion {companion}: {ce}")
        self._docker.remove_container(instance['container_name'])
        # PR #84 C1: deregister the per-instance Authentik provider on delete.
        self._deregister_authentik(instance['agent_type'], instance_id)
        # #612: revoke the instance's rzfz-sk key (no-op on gpustack boxes /
        # instances launched before #612 — no key id on the config).
        _cfg = instance.get('config') or {}
        if isinstance(_cfg, str):
            _cfg = json.loads(_cfg)
        self._revoke_llm_manager_key(_cfg)

        # Remove volumes — primary + companion
        if type_info:
            for field in ('volumes', 'companion_volumes'):
                raw = type_info.get(field) or '[]'
                vol_specs = (json.loads(raw) if isinstance(raw, str) else raw)
                for vol_spec in vol_specs:
                    # rc6.7 #91: skip bind-mount specs — they reference host
                    # paths the manager doesn't own.
                    if 'host_path' in vol_spec:
                        continue
                    vol_name = f"agent-{instance['agent_type']}-{instance['user_slug']}{instance_suffix(instance.get('instance_no'))}-{vol_spec['name_suffix']}"
                    try:
                        self._docker.remove_volume(vol_name)
                    except Exception as ve:
                        logger.warning(f"Failed to remove volume {vol_name}: {ve}")

        # Drop per-instance database
        if type_info and type_info['requires_db']:
            self._drop_instance_db(instance['agent_type'], instance['user_slug'], instance.get('instance_no') or 1)

        self._db.delete_instance(instance_id)
        self._db.log_audit(instance['user_id'], 'delete', instance['agent_type'], instance_id)
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

        current_env = self._docker.get_container_env(container)
        if current_env is None:
            return None

        instance_config = (instance.get('config') or {})
        if isinstance(instance_config, str):
            try:
                instance_config = json.loads(instance_config)
            except Exception:  # noqa: BLE001
                instance_config = {}
        who = username or instance.get('user_id') or instance.get('user_slug') or ''
        try:
            wanted = self._resolve_env(type_info, instance.get('user_slug') or '',
                                       who, instance_config,
                                       instance_id=instance.get('id'))
            wanted = self._inject_user_mcp(wanted, instance.get('agent_type'),
                                           instance.get('user_slug') or '')
        except Exception:  # noqa: BLE001
            return None

        stale = [key for key, value in wanted.items()
                 if current_env.get(key) != str(value)]

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
                return None

        # Hermes carries its endpoint and key in the COMMAND, not the env; an
        # env-only comparison would call a stale hermes in sync.
        wanted_cmd = _resolve_command_llm_placeholders(
            type_info.get('command'), instance_config.get('_llm_manager_key')) or None
        if wanted_cmd:
            current_cmd = self._docker.get_container_command(container)
            if current_cmd is None:
                return None
            if list(current_cmd) != list(wanted_cmd):
                stale.append('(command)')
        return sorted(stale)

    def _resolve_env(self, type_info: dict, user_slug: str,
                     username: str, instance_config: dict,
                     template_field: str = 'env_template',
                     instance_id=None) -> dict:
        """Resolve env_template placeholders to concrete values.

        `template_field` selects which JSONB column to resolve — defaults to
        'env_template' (primary container); pass 'companion_env_template' for
        the bundled companion (M020 S02).

        `instance_id` (M031-FOLLOWUPS B1/B2) — when provided, exposes
        {{instance_hash}} (the 8-hex-char DNS token from caddy_client) so
        env vars like SANDBOX_CONTAINER_URL_PATTERN and AGENT_INSTANCE_HOSTNAME
        can use the same hostname Caddy registers (`<type>-<hash>.agents.<domain>`).
        Fixes the long-standing "openhands-{slug} ≠ openhands-{hash}" mismatch
        and unblocks paperclip's auto-allowlist registration.
        """
        raw = type_info.get(template_field) or '{}'
        template = (json.loads(raw) if isinstance(raw, str) else raw)
        env = {}
        import os
        if not template:
            return env
        # Compute instance_hash if available
        instance_hash = ''
        if instance_id is not None:
            try:
                from app.services.caddy_client import instance_token
                instance_hash = instance_token(instance_id)
            except Exception:
                pass
        for key, value in template.items():
            resolved = str(value)
            resolved = resolved.replace('{{user_slug}}', user_slug)
            resolved = resolved.replace('{{user_id}}', username)
            resolved = resolved.replace('{{instance_hash}}', instance_hash)
            resolved = resolved.replace('{{generated_secret}}',
                                        instance_config.get('_generated_secret', ''))
            resolved = resolved.replace('{{MAIN_DOMAIN}}',
                                        os.environ.get('MAIN_DOMAIN', 'localhost'))
            resolved = resolved.replace('{{AGENTS_DOMAIN}}',
                                        os.environ.get('AGENTS_DOMAIN',
                                            f"agents.{os.environ.get('MAIN_DOMAIN', 'localhost')}"))
            resolved = resolved.replace('{{GPUSTACK_API_KEY}}',
                                        os.environ.get('GPUSTACK_API_KEY', ''))
            resolved = resolved.replace('{{VALKEY_PASSWORD}}',
                                        os.environ.get('VALKEY_PASSWORD', ''))
            # ── Gitea checkout wiring (#165) ─────────────────────────────────
            # {{gitea_token}} → the PER-USER token minted at launch (stored on
            # instance_config so it survives an upgrade/recreate); empty when
            # Gitea is off / the user isn't in Gitea yet. {{GITEA_EXTERNAL_URL}}
            # / {{GITEA_INTERNAL_URL}} give the container the external form to
            # rewrite (insteadOf) and the reachable internal endpoint.
            resolved = resolved.replace('{{gitea_token}}',
                                        instance_config.get('_gitea_token', ''))
            resolved = resolved.replace('{{GITEA_EXTERNAL_URL}}',
                                        _gitea_external_url())
            resolved = resolved.replace('{{GITEA_INTERNAL_URL}}',
                                        _gitea_internal_url())
            resolved = resolved.replace('{{AUTHENTIK_BOOTSTRAP_PASSWORD}}',
                                        os.environ.get('AUTHENTIK_BOOTSTRAP_PASSWORD', ''))
            # Per-instance DB placeholders
            db_name = f"agent_{type_info['id']}_{user_slug}_db"
            db_user = f"agent_{type_info['id']}_{user_slug}"
            db_pass = instance_config.get('_db_password', '')
            resolved = resolved.replace('{{instance_db_name}}', db_name)
            resolved = resolved.replace('{{instance_db_user}}', db_user)
            resolved = resolved.replace('{{instance_db_password}}', db_pass)
            resolved = resolved.replace('{{valkey_db_index}}', '10')
            # #233 — the user's own name for this agent, surfaced INSIDE the
            # container so the terminal titles itself with it.
            resolved = resolved.replace('{{custom_name}}',
                                        instance_config.get('custom_name', ''))
            env[key] = resolved

        # An EMPTY AGENT_LABEL is not the same as an absent one: the container
        # reads `os.environ.get("AGENT_LABEL", AGENT_KIND)`, so passing '' wins
        # over the fallback and titles the window with nothing at all. Drop the
        # key instead, and the image's own default stays in charge.
        if env.get('AGENT_LABEL') == '':
            del env['AGENT_LABEL']

        # #612/#959: LLM-Manager boxes — repoint the LLM backend at the
        # manager's OpenAI ingress with the per-user rzfz-sk key. Only when a
        # key was actually minted (i.e. this IS an LLM-Manager box and the
        # mint succeeded); gpustack boxes keep the catalog defaults untouched.
        _apply_llm_manager_endpoint(env, instance_config.get('_llm_manager_key'))

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

    def _inject_user_mcp(self, env: dict, agent_type: str, user_slug: str) -> dict:
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
        wiring = mcp_manager_client.fetch_user_wiring(user_slug)

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

    def _revoke_llm_manager_key(self, instance_config: dict):
        """#612: revoke the instance's rzfz-sk on delete. Best-effort +
        idempotent (a missing key on the manager side reports success)."""
        kid = (instance_config or {}).get('_llm_manager_key_id')
        if not kid:
            return
        try:
            import httpx
            httpx.post(
                f'http://llm-manager:8080/internal/agent-keys/{kid}/revoke',
                timeout=10)
        except Exception as e:  # noqa: BLE001
            logger.warning("llm-manager key revoke %s failed: %s", kid, e)

    def _mint_gitea_token(self, username: str, user_slug: str) -> str:
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
        token_name = f"coding-agent-{user_slug}-{secrets.token_hex(3)}"
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

    def _create_instance_db(self, agent_type: str, user_slug: str,
                            instance_config: dict, instance_no: int = 1):
        """Create a per-instance PostgreSQL database and user."""
        import os
        sfx = instance_suffix(instance_no).replace('-', '_')   # #1988: _2, _3, ...
        db_name = f"agent_{agent_type}_{user_slug}{sfx}_db"
        db_user = f"agent_{agent_type}_{user_slug}{sfx}"
        db_pass = secrets.token_urlsafe(24)
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
                cur.execute(f"CREATE USER \"{db_user}\" WITH PASSWORD %s", (db_pass,))
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
                cur.execute(f"ALTER USER \"{db_user}\" WITH PASSWORD %s", (db_pass,))
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

    def _drop_instance_db(self, agent_type: str, user_slug: str, instance_no: int = 1):
        """Drop a per-instance PostgreSQL database and user."""
        sfx = instance_suffix(instance_no).replace('-', '_')   # #1988
        db_name = f"agent_{agent_type}_{user_slug}{sfx}_db"
        db_user = f"agent_{agent_type}_{user_slug}{sfx}"

        admin_dsn = self._config['DATABASE_URL'].rsplit('/', 1)[0] + '/postgres'
        conn = psycopg2.connect(admin_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            cur.execute(f'DROP USER IF EXISTS "{db_user}"')
            logger.info(f"Dropped database {db_name} and user {db_user}")
        finally:
            cur.close()
            conn.close()
