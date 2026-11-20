# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Mac LLM backends blueprint (#168 P2) — manage the Mac Ollama gateway.

The whole panel is GUARDED on the ``mac-llm`` profile being enabled (otherwise
it renders an "enable the module first" notice). Super-admin gating is
inherited from the app-wide before_request middleware (app/auth.py) — no
per-route decorator, same as every other portal page.

What it does:
  * lists the configured Macs, parsed back OUT of the gateway ``config.yaml``
    (``_load_macs`` — inverse of the P1 ``gen_config.build_config``), and
    live-pings each Mac's Ollama ``/api/version`` for reachability (2s);
  * add / remove a Mac → rewrites ``config.yaml`` via the P1 config generator
    (reused verbatim, never duplicated) → restarts the gateway through the
    shared ``ApplyManager.apply_service_restart`` (same path as settings.*);
  * Tier-1 Mac model management over the Ollama HTTP API — NO SSH:
    pull (``POST /api/pull``), preload/keep-warm (``POST /api/generate`` with
    ``keep_alive:-1``), list (``GET /api/tags`` + ``GET /api/ps``), delete
    (``DELETE /api/delete``).

Tier-2 SSH host management is P3 (design §7) — NOT built here. The template
leaves a per-Mac "host management (SSH)" seam for it.

Design: .gsd/reports/2026.08-mac-ollama-gateway-design.md
Plan:   .gsd/reports/2026.08-mac-ollama-gateway-plan.md (Phase P2, Tasks 2.1–2.3)
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import threading
import time
import urllib.request
import urllib.parse
import uuid

import requests as http_requests
import yaml
from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

mac_backends_bp = Blueprint('mac_backends', __name__)

PROFILE = 'mac-llm'
GATEWAY_MODULE_REL = 'modules/llm/mac-gateway'
CONFIG_YAML_REL = 'modules/llm/mac-gateway/config.yaml'
GATEWAY_CONTAINER = 'llm-mac-gateway'
DEFAULT_OLLAMA_PORT = 11434

# The gateway's OpenAI-compatible base URL as reached by other stack
# containers (Docker DNS). Sourced from modules/llm/mac-gateway/compose.yml:
# the container is named `llm-mac-gateway`, listens on 4000 (hardcoded via
# `command: ["--config", "/app/config.yaml", "--port", "4000"]` — NOT the
# host-bind `MAC_GATEWAY_PORT`, which only affects the loopback debug port),
# and LiteLLM's OpenAI-compatible path is `/v1` (issue #241: NOT GPUStack's
# `/v1-openai`).
GATEWAY_CONNECTION_STRING = f'http://{GATEWAY_CONTAINER}:4000/v1'

# Explicit "not set" marker for key_masked — never an empty-looking string
# (issue #241: must be visibly distinguishable from a masked real key).
_KEY_NOT_SET_MARKER = 'not set'

# Explicit "configured but too short to mask safely" marker (fix-round-1,
# review CRITICAL): distinct from _KEY_NOT_SET_MARKER so an operator can
# tell "no key" apart from "key present but hidden entirely".
_KEY_TOO_SHORT_MARKER = 'set (too short to display safely)'

# Minimum raw-key length before ANY tail is shown. `raw[-4:]` on a string
# shorter than this reveals the whole string (len<=4) or a disproportionate
# fraction of it (e.g. 4-of-5 chars = 80%); showing the last 4 is only safe
# once those 4 chars are at most ~1/3 of the total. 12 is picked so that, at
# the boundary itself, exactly 4 of 12 chars (1/3) are ever revealed — below
# it, no part of the raw key is shown at all.
_MASK_MIN_LEN = 12

# Ollama HTTP API timeouts (seconds). Pull downloads a model → generous.
_HEALTH_TIMEOUT = 2.0
_PULL_TIMEOUT = 1800
_PRELOAD_TIMEOUT = 300
_LIST_TIMEOUT = 10
_DELETE_TIMEOUT = 30

# Async pull job store (#239). The Config Portal runs gunicorn with a SINGLE
# worker (`-w 1 --timeout 300`, core/config/Dockerfile) — a synchronous pull
# used to block that one worker for up to `_PULL_TIMEOUT` (1800s), hanging the
# ENTIRE portal for every operator, and any pull over 300s got killed
# mid-download by gunicorn's own worker timeout (misleadingly, since Ollama
# kept downloading server-side regardless). The fix: `pull_model` spawns a
# daemon thread and returns a job_id immediately; `pull_status` polls this
# dict. A plain dict + lock is safe here BECAUSE of the single-worker
# constraint above — there is exactly one process, so no cross-process
# coordination (Valkey, a file, ...) is needed.
_PULL_JOBS = {}
_PULL_JOBS_LOCK = threading.Lock()
_PULL_JOB_MAX_AGE_SECONDS = 3600   # evict finished jobs older than this
_PULL_JOB_MAX_COUNT = 50           # cap total tracked jobs (keep newest)

# Validation: an Ollama model alias/tag charset (e.g. "mac-qwen3-coder",
# "qwen3-coder:30b", "llama3.1:8b"). Names shown in Open WebUI.
_ALIAS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
# A DNS hostname label set (RFC-1123-ish) — the non-IP branch of _valid_host.
_HOSTNAME_RE = re.compile(
    r'^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)'
    r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
)


# ---------------------------------------------------------------------------
# Paths + the P1 config generator (reused, never duplicated)
# ---------------------------------------------------------------------------

def _stack_root():
    return current_app.config['STACK_ROOT']


def _config_path():
    return os.path.join(_stack_root(), CONFIG_YAML_REL)


def _gen_config():
    """Load the P1 config generator from the mounted stack root.

    modules/llm/mac-gateway/gen_config.py is the single source of truth for the
    gateway's config.yaml shape. We import the ACTUAL file (it rides along in
    the /stack bind mount) instead of re-implementing build_config/dump_yaml —
    add/remove writes exactly what P1 (and any hand-generation) writes.
    """
    path = os.path.join(_stack_root(), GATEWAY_MODULE_REL, 'gen_config.py')
    spec = importlib.util.spec_from_file_location('mac_gateway_gen_config', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gen_config_or_none():
    """The P1 generator, or None when the mac-gateway module files are absent.

    #1149: `index()` needs the generator to name the sampling profiles and to
    interpret the params in config.yaml — but the panel also renders on a box
    where `mac-llm` is enabled and the module directory has not been laid down
    yet. Before #1149 that path never touched the generator (`_load_macs`
    short-circuits on a missing config.yaml), so making the load unconditional
    turned a fresh enable into a 500. A panel without the profile column is
    degraded; a panel that does not render is broken.

    READ path only. `_save_macs` deliberately keeps using `_gen_config()` — a
    write that cannot go through the single source of truth must fail loudly,
    never fall back to a locally-invented config shape.
    """
    try:
        return _gen_config()
    except Exception:  # noqa: BLE001 - any load failure degrades the same way
        return None


def _profile_enabled():
    return PROFILE in current_app.profile_manager.get_enabled_profiles()


def _gateway_key_set():
    """True if MAC_GATEWAY_MASTER_KEY is non-empty in .env (grep, never source)."""
    try:
        from razzfazz_common.env_utils import read_env_key
    except Exception:  # pragma: no cover - lib always present in-container
        return None
    val = read_env_key(os.path.join(_stack_root(), '.env'), 'MAC_GATEWAY_MASTER_KEY')
    return bool(val)


def _read_gateway_key():
    """Read the raw MAC_GATEWAY_MASTER_KEY value from .env (grep, never source).

    Callers MUST NOT put the return value in any response body — mask it
    first via `_mask_key`. Kept separate from `_gateway_key_set` (which only
    needs a boolean) so callers that DO need the value have one place to get
    it, making the "never leak the raw value" invariant easy to audit.
    """
    try:
        from razzfazz_common.env_utils import read_env_key
    except Exception:  # pragma: no cover - lib always present in-container
        return ''
    return read_env_key(os.path.join(_stack_root(), '.env'), 'MAC_GATEWAY_MASTER_KEY')


def _mask_key(raw):
    """Mask a secret so at most its last 4 characters are ever visible.

    Returns the explicit `_KEY_NOT_SET_MARKER` when `raw` is falsy (unset or
    empty in .env) instead of an empty-looking masked string — an operator
    must be able to tell "no key configured" apart from "key configured but
    hidden".

    Guardrail (fix-round-1, review CRITICAL): for `raw` shorter than
    `_MASK_MIN_LEN`, showing the last 4 characters would either leak the
    ENTIRE value (len<=4 — `raw[-4:] == raw`) or a disproportionate fraction
    of it (e.g. 4-of-5 chars = 80%). Below that length, NO part of `raw` is
    shown — `_KEY_TOO_SHORT_MARKER` is returned instead. At/above it, the
    last 4 chars are shown, which is at most ~1/3 of the total. Never
    returns (or logs) the full `raw` value.

    CFG-27: the mask is FIXED-WIDTH. A length-proportional run of bullets
    disclosed the exact length of MAC_GATEWAY_MASTER_KEY to anyone who could
    read the panel — free information for an offline attack, and worth
    nothing to the operator, who only needs the tail to recognise the key.
    """
    if not raw:
        return _KEY_NOT_SET_MARKER
    if len(raw) < _MASK_MIN_LEN:
        return _KEY_TOO_SHORT_MARKER
    tail = raw[-4:]
    return f'{"•" * 8}{tail}'


# ---------------------------------------------------------------------------
# config.yaml <-> macs (pure)
# ---------------------------------------------------------------------------

def _parse_api_base(base):
    """('http://192.0.2.194:11434/v1') -> ('192.0.2.194', 11434). (None, None) on junk."""
    try:
        p = urllib.parse.urlparse(base or '')
        if not p.hostname:
            return None, None
        return p.hostname, p.port or DEFAULT_OLLAMA_PORT
    except Exception:
        return None, None


def _macs_from_config(cfg, gen=None):
    """Inverse of gen_config.build_config: a LiteLLM config dict -> macs list.

    Groups model_list entries by api_base (ip:port) into one Mac each. The Mac
    ``name`` is NOT persisted by build_config, so it is reconstructed as the ip
    (the stable, round-trip-safe identity used for management + removal).
    Pure — no app/network/filesystem.

    #865: the #242 sampling ``profile`` must survive the round-trip too. It is
    not stored under its own key — build_config writes the profile's SAMPLING
    PARAMS into litellm_params — so recognising it needs the generator's profile
    table, passed in as ``gen`` (keeping this function pure and callable without
    an app context). Without it, an admin's ``profile: extraction`` pin was
    dropped here and the next _save_macs() silently reset that model's
    temperature 0.1 -> 0.7 on a structured-extraction workload.
    """
    recognise = getattr(gen, 'profile_from_params', None) if gen is not None else None
    by_key = {}
    order = []
    for entry in (cfg or {}).get('model_list') or []:
        params = entry.get('litellm_params', {}) or {}
        ip, port = _parse_api_base(params.get('api_base'))
        if ip is None:
            continue
        key = (ip, port)
        if key not in by_key:
            by_key[key] = {'name': ip, 'ip': ip, 'port': port, 'models': []}
            order.append(key)
        model = params.get('model', '') or ''
        tag = model.split('/', 1)[1] if model.startswith('openai/') else model
        parsed = {
            'alias': entry.get('model_name', ''),
            'ollama_tag': tag,
        }
        # Omit the key for the default/unrecognised profile, mirroring
        # build_config's contract (absent == default), so a round-tripped Mac
        # stays equal to one produced by _add_model().
        profile = recognise(params) if recognise else None
        if profile:
            parsed['profile'] = profile
        # #813: the declared task (model_info.mode) must survive too, for the
        # same reason as the profile above — _save_macs() REGENERATES from this
        # structure, so a field not read back here is destroyed by the next
        # unrelated add()/remove(). Losing it is worse than losing the sampling
        # profile: an embedding model reverts to chat and then gets registered
        # with Dify as mode=chat, the broken provider entry review #666 exists
        # to prevent. Read straight from the ENTRY (not the generator's tables),
        # so this works with gen=None as documented; the default is omitted to
        # mirror build_config's absent-==-default contract exactly.
        task = None
        entry_task = (entry.get('model_info') or {}).get('mode')
        if entry_task and entry_task != getattr(gen, 'DEFAULT_TASK', 'chat'):
            task = entry_task
        if task:
            parsed['task'] = task
        by_key[key]['models'].append(parsed)
    return [by_key[k] for k in order]


def _sampling_profile_names(gen):
    """#1149: the selectable sampling profiles, FROM the generator's table.

    Never a local copy: `_sampling_params` silently falls back to the DEFAULT
    profile for a name it does not know, so a stale list here would provision
    chat sampling onto a model the operator pinned to extraction and report
    success. An older generator without the accessor yields an empty list,
    which degrades to the pre-#1149 behaviour (no selector, no profile
    accepted) rather than to a guess.
    """
    names = getattr(gen, 'sampling_profiles', None)
    return list(names()) if callable(names) else []


def _effective_sampling(cfg, gen):
    """#1149: the sampling params each model_list entry ACTUALLY carries.

    Keyed by (ip, port, alias) — the same identity `_macs_from_config` groups
    on. Each value is {params, profile, expected, drift}.

    `params` is read straight out of config.yaml rather than re-derived from
    the recognised profile, because the drift this surfaces is exactly the case
    where the two disagree: a hand-edited config, or one written before #242,
    can carry the Ollama tag's chat-creative Modelfile defaults (temperature
    1.0 / presence_penalty 1.5) — the #242 finding itself. Re-deriving would
    print the value the operator EXPECTS and hide the one the gateway sends.

    `drift` is therefore "what is written != what the recognised profile pins",
    which also covers the pre-#242 entry that pins NOTHING at all and thus
    leaves the Mac's own Modelfile defaults in charge.
    """
    profiles = _sampling_profile_names(gen)
    params_of = getattr(gen, 'sampling_profile_params', None)
    recognise = getattr(gen, 'profile_from_params', None)
    default = getattr(gen, 'DEFAULT_SAMPLING_PROFILE', 'chat')
    if not callable(params_of):
        return {}
    # The sampling surface = every key ANY profile pins, so an extraction-only
    # key (top_p) is still displayed when it lingers on a chat-profile entry.
    keys = set()
    for name in profiles:
        keys.update(params_of(name))

    out = {}
    for entry in (cfg or {}).get('model_list') or []:
        params = entry.get('litellm_params', {}) or {}
        ip, port = _parse_api_base(params.get('api_base'))
        if ip is None:
            continue
        actual = {k: params[k] for k in keys if k in params}
        profile = (recognise(params) if callable(recognise) else None) or default
        expected = params_of(profile)
        out[(ip, port, entry.get('model_name', ''))] = {
            'params': actual,
            'profile': profile,
            'expected': expected,
            'drift': actual != expected,
        }
    return out


def _read_config():
    """Parse the gateway config.yaml (empty dict when absent)."""
    path = _config_path()
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _load_macs():
    """Read config.yaml and return the macs list (empty if absent)."""
    # #865: pass the generator so the sampling profile is recognised on the way
    # in — _save_macs() regenerates FROM the profile, so anything not read back
    # here is silently reset on the next unrelated add()/remove().
    return _macs_from_config(_read_config(), gen=_gen_config())


def _save_macs(macs):
    """Regenerate config.yaml from macs via the P1 generator (atomic write)."""
    gc = _gen_config()
    text = gc.dump_yaml(gc.build_config(macs))
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _add_model(macs, ip, port, alias, ollama_tag, profile=None):
    """Return a NEW macs list with (alias, ollama_tag) added to the ip:port Mac.

    Appends to an existing Mac (same ip:port) or creates one; de-dupes on alias.
    Pure — takes and returns plain data.

    #1149: a falsy `profile` omits the key entirely, mirroring build_config's
    absent-==-DEFAULT contract, so a plain add stays byte-identical to what the
    pre-#1149 form produced. The caller validates the name against the
    generator's table BEFORE getting here — an unrecognised one must never
    reach `build_config`, which would fall back to the default silently.
    """
    port = int(port)
    model = {'alias': alias, 'ollama_tag': ollama_tag}
    if profile:
        model['profile'] = profile
    out = [{'name': m.get('name', m['ip']), 'ip': m['ip'], 'port': int(m['port']),
            'models': list(m['models'])} for m in macs]
    for m in out:
        if m['ip'] == ip and m['port'] == port:
            if any(x['alias'] == alias for x in m['models']):
                return out
            m['models'].append(model)
            return out
    out.append({'name': ip, 'ip': ip, 'port': port, 'models': [model]})
    return out


def _set_model_profile(macs, ip, alias, profile):
    """Return (NEW macs list, found) with the ip Mac's `alias` model re-pinned.

    #1149: `_add_model` de-dupes on alias and returns the list UNCHANGED, so
    re-adding an existing alias cannot change its profile — without this the
    panel can only pin a profile at first provisioning and the operator is back
    to hand-editing config.yaml, which is what the issue was filed for.

    A falsy `profile` clears the pin (back to the generator's DEFAULT). The
    model dict is REPLACED rather than patched, so switching away from
    `extraction` also drops that profile's extra keys instead of leaving a
    stale `top_p` behind. Pure.
    """
    found = False
    out = []
    for m in macs:
        models = []
        for x in m['models']:
            if m['ip'] == ip and x['alias'] == alias:
                found = True
                x = {k: v for k, v in x.items() if k != 'profile'}
                if profile:
                    x['profile'] = profile
            models.append(x)
        out.append({'name': m.get('name', m['ip']), 'ip': m['ip'],
                    'port': int(m['port']), 'models': models})
    return out, found


def _remove_mac(macs, ip):
    """Return a NEW macs list with the ip Mac (all its models) dropped. Pure."""
    return [{'name': m.get('name', m['ip']), 'ip': m['ip'], 'port': int(m['port']),
             'models': list(m['models'])} for m in macs if m['ip'] != ip]


def _find_mac(macs, ident):
    """Find a Mac by its ip identity (route <mac> param / remove key)."""
    for m in macs:
        if m['ip'] == ident:
            return m
    return None


# ---------------------------------------------------------------------------
# Validation (pure)
# ---------------------------------------------------------------------------

def _valid_host(s):
    if not s:
        return False
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(s))


def _valid_port(s):
    try:
        n = int(s)
    except (TypeError, ValueError):
        return False
    return 1 <= n <= 65535


def _valid_alias(s):
    return bool(s) and bool(_ALIAS_RE.match(s))


def _default_profile(gen):
    return getattr(gen, 'DEFAULT_SAMPLING_PROFILE', 'chat')


def _reject_unknown_profile(profile, gen):
    """#1149: refuse a sampling profile the generator does not implement.

    Returns a 4xx response, or None when the profile is acceptable (empty ==
    the generator's DEFAULT, which is how every pre-#1149 form posts).

    Loud on purpose, and the ONLY validator here that does not merely flash and
    redirect: `gen_config._sampling_params` falls back to the DEFAULT profile
    for any name it does not recognise, so passing a typo'd `extractoin`
    through would provision chat-creative sampling onto a structured-extraction
    model and report success — the #242 hazard, re-introduced by the very UI
    built to prevent it. The selector only ever offers names from the
    generator's table, so reaching this is already a hand-crafted POST or a
    genuinely mismatched deployment; neither should be papered over.
    """
    if not profile:
        return None
    known = _sampling_profile_names(gen)
    if profile in known:
        return None
    flash(f'Unknown sampling profile: {profile!r} '
          f'(known: {", ".join(known) or "none"})', 'error')
    return redirect(url_for('mac_backends.index')), 400


# ---------------------------------------------------------------------------
# Live health + Tier-1 Ollama HTTP API (no SSH)
# ---------------------------------------------------------------------------

def _health(ip, port, timeout=_HEALTH_TIMEOUT):
    """Ping GET /api/version. Returns (reachable: bool, version: str|None)."""
    url = f'http://{ip}:{port}/api/version'
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (LAN host)
            if r.status == 200:
                data = json.loads(r.read().decode('utf-8'))
                return True, data.get('version')
    except Exception:
        pass
    return False, None


# --- pure request builders (URL + payload) — unit-tested, no network --------

def _ollama_pull_request(ip, port, model):
    return 'POST', f'http://{ip}:{port}/api/pull', {'name': model, 'stream': False}


def _ollama_pull_stream(ip, port, model):
    """POST /api/pull with stream:True; yields parsed NDJSON progress dicts.

    Deliberately a SEPARATE helper from `_ollama_pull_request`/`_ollama_call`
    (#239) — those two keep serving the non-streaming Tier-1 dispatch pattern
    used by preload/list/delete unchanged. This one drives the async
    background pull worker: a short connect timeout (fail fast if the Mac is
    unreachable) plus a generous read timeout of `_PULL_TIMEOUT` (a slow
    download must not be cut off early), via
    `timeout=(connect, read)`. Raises on transport/HTTP failure — the caller
    (`_run_pull_job`) is responsible for catching it and recording
    `state='error'` on the job.
    """
    url = f'http://{ip}:{port}/api/pull'
    resp = http_requests.post(
        url, json={'name': model, 'stream': True},
        stream=True, timeout=(10, _PULL_TIMEOUT),
    )
    resp.raise_for_status()
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue
    finally:
        resp.close()


def _ollama_preload_request(ip, port, model):
    # keep_alive:-1 pins the model resident (kills cold-start); empty prompt
    # just triggers the load without generating.
    return ('POST', f'http://{ip}:{port}/api/generate',
            {'model': model, 'prompt': '', 'keep_alive': -1})


def _ollama_tags_request(ip, port):
    return 'GET', f'http://{ip}:{port}/api/tags', None


def _ollama_ps_request(ip, port):
    return 'GET', f'http://{ip}:{port}/api/ps', None


def _ollama_delete_request(ip, port, model):
    return 'DELETE', f'http://{ip}:{port}/api/delete', {'name': model}


def _ollama_call(method, url, payload, timeout):
    """Dispatch one Ollama HTTP call. Returns (ok, data_or_text, status)."""
    try:
        resp = http_requests.request(
            method, url,
            json=payload if payload is not None else None,
            timeout=timeout,
        )
    except http_requests.RequestException as e:
        return False, str(e), None
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.ok, data, resp.status_code


def _wants_json():
    return (
        request.headers.get('HX-Request')
        or request.args.get('format') == 'json'
        or request.accept_mimetypes.best == 'application/json'
    )


def _audit(action, target, detail, risk='caution', outcome='success',
           user=None, source_ip=None):
    """Write one audit-log entry.

    `user`/`source_ip` default to the current request's session/headers, but
    can be supplied explicitly. Needed by the async pull worker (#239): it
    runs in a background thread with no Flask request context, so it
    captures both from the request that started it and passes them in here
    instead of touching `session`/`request` off-thread.
    """
    if user is None:
        user = session.get('admin_username', 'admin')
    if source_ip is None:
        source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    current_app.audit_logger.log(
        'config.change', user=user, source_ip=source_ip,
        category='mac-llm', action=action, target=target,
        detail=detail, risk=risk, outcome=outcome,
    )
    return user, source_ip


# ---------------------------------------------------------------------------
# Async pull jobs (#239)
# ---------------------------------------------------------------------------

def _evict_stale_pull_jobs_locked():
    """Bound `_PULL_JOBS`. Caller MUST already hold `_PULL_JOBS_LOCK`.

    Two passes: drop finished (done/error) jobs older than
    `_PULL_JOB_MAX_AGE_SECONDS`, then — if still at/over `_PULL_JOB_MAX_COUNT`
    once the new job about to be inserted is counted — drop the
    oldest-started FINISHED jobs until back under the cap. A box that has
    pulled hundreds of models over its lifetime must not grow this dict
    forever.

    CFG-26: the overflow pass used to drop the oldest jobs *regardless of
    state*, so at the cap a live download's row was deleted — `pull_status`
    then answered `404 Unknown pull job` for a pull still in flight, and
    `_run_pull_job`'s progress writes became silent no-ops (`if job is None:
    continue`), so even the terminal state was never recorded. Running jobs
    are now excluded; if that leaves the dict at the cap the caller is told
    so (it refuses the new job) rather than a live pull being discarded.
    Returns True when there is room for the new job.
    """
    now = time.time()
    stale = [
        jid for jid, job in _PULL_JOBS.items()
        if job['state'] != 'running'
        and job['finished'] is not None
        and (now - job['finished']) > _PULL_JOB_MAX_AGE_SECONDS
    ]
    for jid in stale:
        del _PULL_JOBS[jid]

    overflow = len(_PULL_JOBS) - _PULL_JOB_MAX_COUNT + 1
    if overflow > 0:
        evictable = [jid for jid, job in _PULL_JOBS.items()
                     if job['state'] != 'running']
        oldest = sorted(evictable, key=lambda jid: _PULL_JOBS[jid]['started'])[:overflow]
        for jid in oldest:
            del _PULL_JOBS[jid]

    return len(_PULL_JOBS) < _PULL_JOB_MAX_COUNT


def _run_pull_job(app, job_id, ip, port, model, user, source_ip):
    """Background worker for one async model pull (#239).

    Runs in a daemon thread spawned by `pull_model`, which has already
    returned to its caller — this function is the ONLY place that blocks on
    the (potentially many-minute) Ollama download. No Flask request context
    exists here (the thread outlives the request that started it), so an app
    context is pushed explicitly for `current_app`/`_audit` to resolve, and
    `user`/`source_ip` are passed in (captured from the real request before
    the thread started) rather than read from `session`/`request`.

    Every exception is caught — a background thread that dies silently would
    leave the job stuck at state='running' forever with no error ever
    surfaced to the operator.
    """
    with app.app_context():
        try:
            # CFG-6: Ollama reports a failed pull IN-BAND — HTTP 200 with a
            # terminal `{"error": "pull model manifest: file does not exist"}`
            # NDJSON line. `raise_for_status()` only sees transport/HTTP
            # failures, so a mistyped model used to end the loop normally,
            # set state='done' and audit outcome='success'. Track the last
            # error line and whether any chunk reported success.
            stream_error = None
            saw_success = False
            for chunk in _ollama_pull_stream(ip, port, model):
                if not isinstance(chunk, dict):
                    continue
                err = chunk.get('error')
                if err:
                    stream_error = str(err)
                status = chunk.get('status')
                if status == 'success':
                    saw_success = True
                completed = chunk.get('completed')
                total = chunk.get('total')
                with _PULL_JOBS_LOCK:
                    job = _PULL_JOBS.get(job_id)
                    if job is None:
                        continue
                    if status is not None:
                        job['status'] = status
                    if completed is not None:
                        try:
                            job['completed'] = int(completed)
                        except (TypeError, ValueError):
                            pass
                    if total is not None:
                        try:
                            job['total'] = int(total)
                        except (TypeError, ValueError):
                            pass
            # CFG-6: a stream that carried an `error` line, or that ended
            # without any `status == "success"` chunk, did NOT pull the
            # model — record it as the failure it is instead of auditing a
            # success the operator will later find never happened.
            if stream_error or not saw_success:
                failure = stream_error or (
                    'pull stream ended without a success status')
                with _PULL_JOBS_LOCK:
                    job = _PULL_JOBS.get(job_id)
                    if job is not None:
                        job['state'] = 'error'
                        job['error'] = failure
                        job['finished'] = time.time()
                _audit('pull', ip, f'Pull {model} on {ip} failed: {failure}',
                       outcome='failure', user=user, source_ip=source_ip)
                return
            with _PULL_JOBS_LOCK:
                job = _PULL_JOBS.get(job_id)
                if job is not None:
                    job['state'] = 'done'
                    job['finished'] = time.time()
            _audit('pull', ip, f'Pull {model} on {ip}', outcome='success',
                   user=user, source_ip=source_ip)
        except Exception as e:  # noqa: BLE001 - must never die silently (#239)
            with _PULL_JOBS_LOCK:
                job = _PULL_JOBS.get(job_id)
                if job is not None:
                    job['state'] = 'error'
                    job['error'] = str(e)
                    job['finished'] = time.time()
            try:
                _audit('pull', ip, f'Pull {model} on {ip} failed: {e}',
                       outcome='failure', user=user, source_ip=source_ip)
            except Exception:  # pragma: no cover - audit must not crash the thread
                pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@mac_backends_bp.route('/')
def index():
    if not _profile_enabled():
        # sampling_profiles is still supplied (empty) so the template's loop
        # has its key in both branches.
        return render_template('mac_backends/index.html', enabled=False,
                               macs=[], gateway_key_set=None,
                               sampling_profiles=[])
    # #1149: one generator load + one config read for the whole render — the
    # effective-params column needs the RAW entries, which _macs_from_config
    # deliberately does not carry (adding a field there would be regenerated
    # back into config.yaml by _save_macs).
    gen = _gen_config_or_none()
    cfg = _read_config()
    effective = _effective_sampling(cfg, gen)
    rows = []
    for mac in _macs_from_config(cfg, gen=gen):
        reachable, version = _health(mac['ip'], mac['port'])
        models = [dict(m, sampling=effective.get(
            (mac['ip'], mac['port'], m['alias']), {})) for m in mac['models']]
        rows.append({'mac': mac, 'reachable': reachable, 'version': version,
                     'models': models})
    return render_template('mac_backends/index.html', enabled=True,
                           macs=rows, gateway_key_set=_gateway_key_set(),
                           sampling_profiles=_sampling_profile_names(gen))


@mac_backends_bp.route('/connection', methods=['GET'])
def connection():
    """Surface the (masked) gateway master key + connection string (#241).

    Read-only, JSON. The full MAC_GATEWAY_MASTER_KEY value is read once
    server-side (`_read_gateway_key`) purely to compute the mask — it is
    NEVER placed in the response body. Only `_mask_key`'s output goes out.

    CFG-27: `add`, `remove` and every Tier-1 route gate on `_profile_enabled`
    (directly or through `_resolve_mac_or_400`); this one did not, so it
    answered with the masked key and the gateway connection string even with
    `mac-llm` switched off.
    """
    if not _profile_enabled():
        return jsonify({'ok': False,
                        'summary': 'The mac-llm module is not enabled.'}), 404
    raw_key = _read_gateway_key()
    return jsonify({
        'key_masked': _mask_key(raw_key),
        'connection_string': GATEWAY_CONNECTION_STRING,
        'api_base': GATEWAY_CONNECTION_STRING,
    })


@mac_backends_bp.route('/add', methods=['POST'])
def add():
    if not _profile_enabled():
        flash('Enable the mac-llm module first.', 'error')
        return redirect(url_for('mac_backends.index'))

    ip = (request.form.get('ip') or '').strip()
    port = (request.form.get('port') or str(DEFAULT_OLLAMA_PORT)).strip()
    alias = (request.form.get('alias') or '').strip()
    tag = (request.form.get('ollama_tag') or '').strip()
    profile = (request.form.get('profile') or '').strip()

    if not _valid_host(ip):
        flash(f'Invalid Mac IP / hostname: {ip!r}', 'error')
        return redirect(url_for('mac_backends.index'))
    if not _valid_port(port):
        flash(f'Invalid port: {port!r} (1–65535)', 'error')
        return redirect(url_for('mac_backends.index'))
    if not _valid_alias(alias):
        flash(f'Invalid model alias: {alias!r}', 'error')
        return redirect(url_for('mac_backends.index'))
    if not _valid_alias(tag):
        flash(f'Invalid Ollama tag: {tag!r}', 'error')
        return redirect(url_for('mac_backends.index'))
    gen = _gen_config()
    err = _reject_unknown_profile(profile, gen)
    if err:
        return err

    macs = _add_model(_load_macs(), ip, int(port), alias, tag,
                      profile=profile or None)
    _save_macs(macs)
    user, source_ip = _audit(
        'add', f'{ip}:{port}',
        f'Added Mac backend {ip}:{port} model {alias} ({tag}, '
        f'sampling profile {profile or _default_profile(gen)})')

    action_id, err = current_app.apply_manager.apply_service_restart(
        [GATEWAY_CONTAINER], f'Updated Mac LLM backends (add {alias}@{ip})',
        user, source_ip, category='mac-llm', risk='caution')
    if err:
        flash(f'Backend saved but gateway restart failed: {err}', 'error')
    else:
        flash(f'Added {alias} on {ip}:{port}. Gateway reloading...', 'success')
    return redirect(url_for('mac_backends.index'))


@mac_backends_bp.route('/remove', methods=['POST'])
def remove():
    if not _profile_enabled():
        flash('Enable the mac-llm module first.', 'error')
        return redirect(url_for('mac_backends.index'))

    ip = (request.form.get('ip') or '').strip()
    macs = _load_macs()
    if _find_mac(macs, ip) is None:
        flash(f'No configured Mac backend at {ip}.', 'error')
        return redirect(url_for('mac_backends.index'))

    _save_macs(_remove_mac(macs, ip))
    user, source_ip = _audit('remove', ip, f'Removed Mac backend {ip}')

    action_id, err = current_app.apply_manager.apply_service_restart(
        [GATEWAY_CONTAINER], f'Updated Mac LLM backends (remove {ip})',
        user, source_ip, category='mac-llm', risk='caution')
    if err:
        flash(f'Backend removed but gateway restart failed: {err}', 'error')
    else:
        flash(f'Removed Mac backend {ip}. Gateway reloading...', 'success')
    return redirect(url_for('mac_backends.index'))


@mac_backends_bp.route('/set-profile', methods=['POST'])
def set_profile():
    """#1149: re-pin an already-configured model's sampling profile.

    The counterpart to the profile argument on `add()`: `_add_model` de-dupes
    on alias, so re-adding cannot change one. Without this route the profile is
    settable only at first provisioning and everything after is a hand-edit of
    config.yaml — the state #1149 exists to end.
    """
    if not _profile_enabled():
        flash('Enable the mac-llm module first.', 'error')
        return redirect(url_for('mac_backends.index'))

    ip = (request.form.get('ip') or '').strip()
    alias = (request.form.get('alias') or '').strip()
    profile = (request.form.get('profile') or '').strip()
    gen = _gen_config()
    err = _reject_unknown_profile(profile, gen)
    if err:
        return err

    macs, found = _set_model_profile(_load_macs(), ip, alias, profile)
    if not found:
        flash(f'No model {alias!r} configured on Mac {ip}.', 'error')
        return redirect(url_for('mac_backends.index')), 404

    _save_macs(macs)
    effective = profile or _default_profile(gen)
    user, source_ip = _audit(
        'set-profile', f'{ip}/{alias}',
        f'Set sampling profile of {alias} on {ip} to {effective}')

    action_id, err = current_app.apply_manager.apply_service_restart(
        [GATEWAY_CONTAINER], f'Updated Mac LLM sampling profile ({alias}@{ip})',
        user, source_ip, category='mac-llm', risk='caution')
    if err:
        flash(f'Profile saved but gateway restart failed: {err}', 'error')
    else:
        flash(f'{alias} on {ip} now uses the {effective} sampling profile. '
              f'Gateway reloading...', 'success')
    return redirect(url_for('mac_backends.index'))


def _resolve_mac_or_400(mac_ident):
    """Shared guard for the Tier-1 routes: profile on + Mac exists → the Mac dict."""
    if not _profile_enabled():
        return None, ('Enable the mac-llm module first.', 400)
    mac = _find_mac(_load_macs(), mac_ident)
    if mac is None:
        return None, (f'No configured Mac backend at {mac_ident}.', 404)
    return mac, None


def _tier1_respond(ok, summary, data, redirect_flash='success'):
    """JSON for HTMX/API callers; flash + redirect for the plain-form baseline."""
    if _wants_json():
        return jsonify({'ok': ok, 'summary': summary, 'data': data}), (200 if ok else 502)
    flash(summary, redirect_flash if ok else 'error')
    return redirect(url_for('mac_backends.index'))


@mac_backends_bp.route('/<mac>/pull', methods=['POST'])
def pull_model(mac):
    """Tier-1: pull a model onto the Mac — ASYNC (#239).

    The Config Portal runs gunicorn with a single worker (`-w 1 --timeout
    300`, core/config/Dockerfile), so this must NEVER block waiting for the
    download: validate, spawn a background thread that does the actual pull
    (`_run_pull_job` + `_ollama_pull_stream`), and return immediately with a
    `job_id` — poll `pull_status` for progress. The plain-form (non-JSON)
    path still works exactly the same way: the pull already started in the
    background by the time this flashes + redirects, so a non-JS client
    gets a working fire-and-forget pull, just without live progress.
    """
    m, err = _resolve_mac_or_400(mac)
    if err:
        return (jsonify({'ok': False, 'summary': err[0]}), err[1]) if _wants_json() else (
            flash(err[0], 'error') or redirect(url_for('mac_backends.index')))
    model = (request.form.get('model') or '').strip()
    if not _valid_alias(model):
        return _tier1_respond(False, f'Invalid model name: {model!r}', None)

    job_id = uuid.uuid4().hex
    user = session.get('admin_username', 'admin')
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    job = {
        'job_id': job_id, 'mac': m['ip'], 'model': model,
        'state': 'running', 'status': None, 'completed': 0, 'total': 0,
        'error': None, 'started': time.time(), 'finished': None,
    }
    with _PULL_JOBS_LOCK:
        # CFG-26: refuse explicitly when the cap is full of RUNNING pulls
        # rather than silently discarding one of them to make room.
        if not _evict_stale_pull_jobs_locked():
            return _tier1_respond(
                False,
                f'Too many pulls in flight ({_PULL_JOB_MAX_COUNT}); '
                f'wait for one to finish and retry.', None)
        _PULL_JOBS[job_id] = job

    app_obj = current_app._get_current_object()
    # #1312: the ACCEPTANCE of the pull is audited here, at request time, not
    # only its outcome in the worker below. Until now a pull that never
    # finished — portal restarted, container recreated, box rebooted — left NO
    # trace at all: who asked for what, and when, was lost with the thread.
    # `test_every_mutating_route_calls_audit_log` reads that as a mutating
    # route without an audit entry, and it is right to: an audit trail that
    # only records completions cannot answer "who started this".
    _audit('pull-requested', m['ip'], f'Pull {model} on {m["ip"]} requested (job {job_id})',
           risk='caution', outcome='success', user=user, source_ip=source_ip)
    threading.Thread(
        target=_run_pull_job,
        args=(app_obj, job_id, m['ip'], m['port'], model, user, source_ip),
        daemon=True,
    ).start()

    summary = f'Pull of {model} started'
    if _wants_json():
        return jsonify({'ok': True, 'job_id': job_id, 'summary': summary}), 202
    flash(summary, 'success')
    return redirect(url_for('mac_backends.index'))


@mac_backends_bp.route('/<mac>/pull-status/<job_id>', methods=['GET'])
def pull_status(mac, job_id):
    """Tier-1: poll an async pull job started by `pull_model` (#239). JSON."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return jsonify({'ok': False, 'summary': err[0]}), err[1]
    with _PULL_JOBS_LOCK:
        job = _PULL_JOBS.get(job_id)
        job = dict(job) if job is not None else None
    # CFG-21: the job must belong to the Mac in the URL — otherwise
    # /mac-backends/<any-mac>/pull-status/<id> reports any other Mac's job.
    if job is None or job.get('mac') != m['ip']:
        return jsonify({'ok': False, 'summary': f'Unknown pull job {job_id!r}'}), 404
    return jsonify({'ok': True, 'job': job})


@mac_backends_bp.route('/<mac>/preload', methods=['POST'])
def preload_model(mac):
    """Tier-1: preload / keep-warm a model (POST /api/generate keep_alive:-1)."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return (jsonify({'ok': False, 'summary': err[0]}), err[1]) if _wants_json() else (
            flash(err[0], 'error') or redirect(url_for('mac_backends.index')))
    model = (request.form.get('model') or '').strip()
    if not _valid_alias(model):
        return _tier1_respond(False, f'Invalid model name: {model!r}', None)
    method, url, payload = _ollama_preload_request(m['ip'], m['port'], model)
    ok, data, status = _ollama_call(method, url, payload, _PRELOAD_TIMEOUT)
    _audit('preload', f"{m['ip']}", f'Preload {model} on {m["ip"]}',
           outcome='success' if ok else 'failure')
    summary = (f'Preloaded {model} (resident) on {m["ip"]}.' if ok
               else f'Preload of {model} failed: {data}')
    return _tier1_respond(ok, summary, data)


@mac_backends_bp.route('/<mac>/models', methods=['GET'])
def list_models(mac):
    """Tier-1: list installed (/api/tags) + running (/api/ps) models. JSON."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return jsonify({'ok': False, 'summary': err[0]}), err[1]
    _, tags_url, _ = _ollama_tags_request(m['ip'], m['port'])
    _, ps_url, _ = _ollama_ps_request(m['ip'], m['port'])
    tags_ok, tags, _ = _ollama_call('GET', tags_url, None, _LIST_TIMEOUT)
    ps_ok, ps, _ = _ollama_call('GET', ps_url, None, _LIST_TIMEOUT)
    return jsonify({
        'ok': tags_ok and ps_ok,
        'ip': m['ip'],
        'tags': tags if tags_ok else None,
        'ps': ps if ps_ok else None,
    }), (200 if (tags_ok and ps_ok) else 502)


@mac_backends_bp.route('/<mac>/delete-model', methods=['POST'])
def delete_model(mac):
    """Tier-1: delete a model from the Mac (DELETE /api/delete)."""
    m, err = _resolve_mac_or_400(mac)
    if err:
        return (jsonify({'ok': False, 'summary': err[0]}), err[1]) if _wants_json() else (
            flash(err[0], 'error') or redirect(url_for('mac_backends.index')))
    model = (request.form.get('model') or '').strip()
    if not _valid_alias(model):
        return _tier1_respond(False, f'Invalid model name: {model!r}', None)
    method, url, payload = _ollama_delete_request(m['ip'], m['port'], model)
    ok, data, status = _ollama_call(method, url, payload, _DELETE_TIMEOUT)
    _audit('delete-model', f"{m['ip']}", f'Delete {model} on {m["ip"]}',
           risk='danger', outcome='success' if ok else 'failure')
    summary = (f'Deleted {model} from {m["ip"]}.' if ok
               else f'Delete of {model} failed: {data}')
    return _tier1_respond(ok, summary, data)
